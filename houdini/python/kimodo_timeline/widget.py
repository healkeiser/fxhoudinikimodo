"""Kimodo Timeline: PySide6 view over model.Timeline, bound to a Kimodo Motion node.

View controls follow the usual DCC timeline conventions:
  wheel            zoom around the cursor
  middle-drag      pan
  F / Home / Fit   fit the whole timeline
  left-drag ruler  scrub the Houdini frame
"""
from __future__ import annotations

import hou
from PySide6 import QtCore, QtGui, QtWidgets

from . import bridge
from .model import TRACKS, TRACK_LABELS, Timeline

# ── layout constants (pixels) ──────────────────────────────────────────────────
GUTTER = 78          # track labels
RULER_H = 22
PROMPT_H = 46
TRACK_H = 20
PAD_R = 12
EDGE_GRAB = 6        # px from a segment's right edge that counts as "resize"
KEY_R = 6            # key marker half-size
MIN_PPF, MAX_PPF = 0.25, 240.0   # zoom limits, pixels per frame
FIT_MARGIN = 0.15                # fit leaves this much of the total free on the right

SEG_COLORS = [QtGui.QColor(c) for c in ("#3b7dd8", "#d65a5a", "#3faa6d", "#d9a13a", "#8a5fd3", "#2fa9b8")]
TRACK_COLORS = {
    "fullbody": QtGui.QColor("#f2c14e"), "LeftHand": QtGui.QColor("#6fb1ff"),
    "RightHand": QtGui.QColor("#ff8a65"), "LeftFoot": QtGui.QColor("#7ed491"),
    "RightFoot": QtGui.QColor("#d98cf0"),
}
BG = QtGui.QColor("#2b2b2b"); ROW_BG = QtGui.QColor("#333333"); GRID = QtGui.QColor("#454545")
TEXT = QtGui.QColor("#e6e6e6"); DIM = QtGui.QColor("#9a9a9a"); PLAYHEAD = QtGui.QColor("#ff5252")
WHITE = QtGui.QColor("#ffffff"); OUTSIDE = QtGui.QColor(0, 0, 0, 60)


class PromptDialog(QtWidgets.QDialog):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Segment prompt")
        self.resize(480, 160)
        lay = QtWidgets.QVBoxLayout(self)
        self.edit = QtWidgets.QPlainTextEdit(text)
        self.edit.setPlaceholderText("What the character does, in English…")
        lay.addWidget(self.edit)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.edit.setFocus()

    def text(self) -> str:
        return self.edit.toPlainText().strip()


class Canvas(QtWidgets.QWidget):
    """Ruler + prompt row + one row per track. Emits `edited` after a model change that
    should be written to the node, `frameRequested` when the ruler is scrubbed."""
    edited = QtCore.Signal(str)          # undo label
    frameRequested = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tl = Timeline()
        self.start = 1
        self.playhead = 1
        # view state: frame at the left edge of the work zone and pixels per frame
        self._view_start = 0.0
        self._ppf = 10.0
        self._fitted = False
        # interaction state
        self._mode = None                 # None | "resize" | "move" | "key" | "scrub" | "pan"
        self._idx = -1                    # segment index being edited
        self._track = None                # track name for key drags
        self._key = None                  # current key frame for key drags
        self._drop = None                 # drop index while moving a segment
        self._pan_x = 0.0
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMinimumHeight(RULER_H + PROMPT_H + TRACK_H * len(TRACKS) + 8)
        self.setContextMenuPolicy(QtCore.Qt.DefaultContextMenu)

    # ── view ─────────────────────────────────────────────────────────────────
    def _work_w(self) -> float:
        return max(1.0, self.width() - GUTTER - PAD_R)

    def fit(self):
        """Show the whole timeline plus some free room on the right."""
        span = max(self.tl.total_frames, 24) * (1.0 + FIT_MARGIN)
        self._ppf = max(MIN_PPF, min(MAX_PPF, self._work_w() / span))
        self._view_start = self.start - span * 0.02
        self._fitted = True
        self.update()

    def x_of(self, frame: float) -> float:
        return GUTTER + (frame - self._view_start) * self._ppf

    def frame_at(self, x: float) -> int:
        return int(round(self._view_start + (x - GUTTER) / self._ppf))

    def zoom(self, factor: float, anchor_x: float):
        f_anchor = self._view_start + (anchor_x - GUTTER) / self._ppf
        self._ppf = max(MIN_PPF, min(MAX_PPF, self._ppf * factor))
        self._view_start = f_anchor - (anchor_x - GUTTER) / self._ppf
        self._fitted = False
        self.update()

    def pan(self, dx_px: float):
        self._view_start -= dx_px / self._ppf
        self._fitted = False
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._fitted:
            self.fit()

    def wheelEvent(self, ev):
        delta = ev.angleDelta().y() or ev.angleDelta().x()
        if delta:
            self.zoom(1.15 ** (delta / 120.0), ev.position().x())
        ev.accept()

    def keyPressEvent(self, ev):
        if ev.key() in (QtCore.Qt.Key_F, QtCore.Qt.Key_Home):
            self.fit()
        else:
            super().keyPressEvent(ev)

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _row_of(self, y: float):
        if y < RULER_H:
            return "ruler", None
        if y < RULER_H + PROMPT_H:
            return "prompt", None
        i = int((y - RULER_H - PROMPT_H) // TRACK_H)
        return ("track", TRACKS[i]) if 0 <= i < len(TRACKS) else (None, None)

    def _track_y(self, track: str) -> float:
        return RULER_H + PROMPT_H + TRACKS.index(track) * TRACK_H + TRACK_H / 2

    def _seg_rects(self):
        out = []
        for i, st in enumerate(self.tl.starts(self.start)):
            x0, x1 = self.x_of(st), self.x_of(st + self.tl.segments[i].frames)
            out.append(QtCore.QRectF(x0, RULER_H + 4, x1 - x0, PROMPT_H - 8))
        return out

    def _seg_at(self, pos):
        if pos.x() < GUTTER:
            return -1, None
        for i, r in enumerate(self._seg_rects()):
            if r.contains(pos):
                return i, r
        return -1, None

    def _key_at(self, track, x):
        for k in self.tl.tracks.get(track, []):
            if abs(self.x_of(k) - x) <= KEY_R + 2:
                return k
        return None

    # ── painting ─────────────────────────────────────────────────────────────
    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, BG)
        work = QtCore.QRectF(GUTTER, 0, w - GUTTER - PAD_R, h)

        # row backgrounds + labels (labels outside the clip so they never scroll)
        p.fillRect(QtCore.QRectF(GUTTER, RULER_H, work.width(), PROMPT_H), ROW_BG)
        for i, t in enumerate(TRACKS):
            y = RULER_H + PROMPT_H + i * TRACK_H
            p.fillRect(QtCore.QRectF(GUTTER, y, work.width(), TRACK_H), ROW_BG if i % 2 else BG)
            p.setPen(DIM)
            p.drawText(QtCore.QRectF(4, y, GUTTER - 8, TRACK_H), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, TRACK_LABELS[t])
        p.setPen(DIM)
        p.drawText(QtCore.QRectF(4, RULER_H, GUTTER - 8, PROMPT_H), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, "Prompts")

        p.setClipRect(work)

        # outside-the-clip shading (before Start Frame)
        x_start = self.x_of(self.start)
        if x_start > GUTTER:
            p.fillRect(QtCore.QRectF(GUTTER, RULER_H, x_start - GUTTER, h - RULER_H), OUTSIDE)

        # ruler + grid: tick every N frames so labels stay ≥ 48 px apart
        step = 1200
        for cand in (1, 2, 5, 10, 12, 24, 25, 30, 48, 50, 60, 100, 120, 240, 300, 600):
            if cand * self._ppf >= 48:
                step = cand; break
        f0 = self.frame_at(GUTTER); f1 = self.frame_at(w - PAD_R)
        f = f0 - (f0 % step)
        while f <= f1:
            x = self.x_of(f)
            p.setPen(GRID); p.drawLine(QtCore.QPointF(x, RULER_H), QtCore.QPointF(x, h))
            p.setPen(TEXT); p.drawText(QtCore.QPointF(x + 3, RULER_H - 7), str(f))
            f += step

        # segments
        rects = self._seg_rects()
        for i, r in enumerate(rects):
            col = SEG_COLORS[i % len(SEG_COLORS)]
            if self._mode == "move" and i == self._idx:
                col = QtGui.QColor(col); col.setAlpha(110)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(col)
            p.drawRoundedRect(r.adjusted(1, 0, -1, 0), 4, 4)
            seg = self.tl.segments[i]
            secs = seg.frames / bridge.fps()
            txt = QtCore.QRectF(r.left() + 6, r.top() + 2, max(0, r.width() - 12), r.height() - 4)
            if txt.width() > 24:
                p.setPen(WHITE)
                p.drawText(txt, QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop | QtCore.Qt.TextSingleLine,
                           p.fontMetrics().elidedText(seg.prompt or "(empty prompt)", QtCore.Qt.ElideRight, int(txt.width())))
                p.setPen(QtGui.QColor(255, 255, 255, 170))
                p.drawText(txt, QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom, f"{seg.frames} f · {secs:.2f} s")
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 120), 2))
            gx = r.right() - 3
            p.drawLine(QtCore.QPointF(gx, r.top() + 8), QtCore.QPointF(gx, r.bottom() - 8))
        if not rects:
            p.setPen(DIM)
            p.drawText(QtCore.QRectF(GUTTER, RULER_H, work.width(), PROMPT_H), QtCore.Qt.AlignCenter,
                       "Right-click to add a prompt segment")
        if self.tl.transition_frames and len(rects) > 1:
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 90), 1, QtCore.Qt.DashLine))
            for r in rects[1:]:
                p.drawLine(QtCore.QPointF(r.left(), RULER_H), QtCore.QPointF(r.left(), RULER_H + PROMPT_H))
        if self._mode == "move" and self._drop is not None:
            starts = self.tl.starts(self.start)
            x = self.x_of(starts[self._drop]) if self._drop < len(starts) else self.x_of(self.start + self.tl.total_frames)
            p.setPen(QtGui.QPen(WHITE, 3))
            p.drawLine(QtCore.QPointF(x, RULER_H + 2), QtCore.QPointF(x, RULER_H + PROMPT_H - 2))

        # keys
        for t in TRACKS:
            y = self._track_y(t)
            col = TRACK_COLORS[t]
            for k in self.tl.tracks.get(t, []):
                x = self.x_of(k)
                path = QtGui.QPainterPath()
                path.moveTo(x, y - KEY_R); path.lineTo(x + KEY_R, y); path.lineTo(x, y + KEY_R); path.lineTo(x - KEY_R, y); path.closeSubpath()
                p.setPen(QtGui.QPen(QtGui.QColor("#111111"), 1)); p.setBrush(col); p.drawPath(path)

        # playhead
        x = self.x_of(self.playhead)
        p.setPen(QtGui.QPen(PLAYHEAD, 2)); p.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, h))
        p.setBrush(PLAYHEAD); p.setPen(QtCore.Qt.NoPen)
        p.drawRect(QtCore.QRectF(x - 14, 2, 28, RULER_H - 6))
        p.setPen(WHITE); p.drawText(QtCore.QRectF(x - 14, 2, 28, RULER_H - 6), QtCore.Qt.AlignCenter, str(self.playhead))
        p.end()

    # ── mouse ────────────────────────────────────────────────────────────────
    def mousePressEvent(self, ev):
        pos = ev.position()
        if ev.button() == QtCore.Qt.MiddleButton:
            self._mode = "pan"; self._pan_x = pos.x(); self.setCursor(QtCore.Qt.ClosedHandCursor); return
        if ev.button() != QtCore.Qt.LeftButton:
            return super().mousePressEvent(ev)
        row, track = self._row_of(pos.y())
        if row == "ruler":
            self._mode = "scrub"; self.frameRequested.emit(self.frame_at(pos.x())); return
        if row == "prompt":
            i, r = self._seg_at(pos)
            if i >= 0:
                self._idx = i
                self._mode = "resize" if pos.x() >= r.right() - EDGE_GRAB - 2 else "move"
                self._drop = i
                self.update()
            return
        if row == "track":
            k = self._key_at(track, pos.x())
            if k is not None:
                self._mode, self._track, self._key = "key", track, k

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._mode == "pan":
            self.pan(pos.x() - self._pan_x); self._pan_x = pos.x(); return
        if self._mode == "scrub":
            self.frameRequested.emit(self.frame_at(pos.x())); return
        if self._mode == "resize":
            st = self.tl.starts(self.start)[self._idx]
            self.tl.resize(self._idx, self.frame_at(pos.x()) - st)
            self.update(); return
        if self._mode == "move":
            f = self.frame_at(pos.x())
            starts = self.tl.starts(self.start)
            drop = len(starts)
            for i, st in enumerate(starts):
                if f < st + self.tl.segments[i].frames / 2:
                    drop = i; break
            self._drop = drop; self.update(); return
        if self._mode == "key":
            new = max(self.start, min(self.frame_at(pos.x()), self.start + self.tl.total_frames - 1))
            self._key = self.tl.move_key(self._track, self._key, new)
            self.update(); return
        # hover cursor
        row, track = self._row_of(pos.y())
        cur = QtCore.Qt.ArrowCursor
        if row == "prompt":
            i, r = self._seg_at(pos)
            if i >= 0:
                cur = QtCore.Qt.SizeHorCursor if pos.x() >= r.right() - EDGE_GRAB - 2 else QtCore.Qt.OpenHandCursor
        elif row == "track" and self._key_at(track, pos.x()) is not None:
            cur = QtCore.Qt.SizeHorCursor
        self.setCursor(cur)

    def mouseReleaseEvent(self, ev):
        mode, self._mode = self._mode, None
        if mode == "pan":
            self.setCursor(QtCore.Qt.ArrowCursor)
        elif mode == "resize":
            self.tl.clamp_keys(self.start); self.edited.emit("Kimodo timeline: resize segment")
        elif mode == "move":
            drop = self._drop if self._drop is not None else self._idx
            if drop > self._idx:
                drop -= 1                    # removing src shifts later indices left
            if drop != self._idx:
                self.tl.move(self._idx, drop); self.edited.emit("Kimodo timeline: reorder segments")
            self._drop = None
        elif mode == "key":
            self.edited.emit("Kimodo timeline: move key")
        self.update()

    def mouseDoubleClickEvent(self, ev):
        pos = ev.position()
        row, _ = self._row_of(pos.y())
        if row == "prompt":
            i, _ = self._seg_at(pos)
            if i >= 0:
                self._mode = None
                self.edit_prompt(i)
        elif row == "ruler":
            self.fit()

    def contextMenuEvent(self, ev):
        pos = QtCore.QPointF(ev.pos())
        row, track = self._row_of(pos.y())
        menu = QtWidgets.QMenu(self)
        if row == "prompt":
            i, _ = self._seg_at(pos)
            if i >= 0:
                menu.addAction("Edit prompt…", lambda: self.edit_prompt(i))
                menu.addAction("Add segment after", lambda: self.add_segment(after=i))
                menu.addAction("Split at playhead", lambda: self.split_at(i, self.playhead))
                menu.addSeparator()
                menu.addAction("Delete segment", lambda: self.remove_segment(i))
            else:
                menu.addAction("Add segment at end", lambda: self.add_segment())
        elif row == "track":
            f = self.frame_at(pos.x())
            k = self._key_at(track, pos.x())
            if k is not None:
                menu.addAction(f"Delete key at {k}", lambda: self.remove_key(track, k))
            else:
                menu.addAction(f"Add {TRACK_LABELS[track]} key at {f}", lambda: self.add_key(track, f))
            menu.addAction(f"Add {TRACK_LABELS[track]} key at playhead ({self.playhead})", lambda: self.add_key(track, self.playhead))
            if self.tl.tracks.get(track):
                menu.addSeparator(); menu.addAction("Clear track", lambda: self.clear_track(track))
        else:
            return
        menu.addSeparator()
        menu.addAction("Fit timeline  (F)", self.fit)
        menu.exec(ev.globalPos())

    # ── model edits (each ends in one undoable write) ────────────────────────
    def edit_prompt(self, i):
        dlg = PromptDialog(self.tl.segments[i].prompt, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.tl.set_prompt(i, dlg.text()); self.edited.emit("Kimodo timeline: edit prompt"); self.update()

    def add_segment(self, after=None):
        dlg = PromptDialog("", self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            default = self.tl.segments[after].frames if after is not None and self.tl.segments else int(round(3 * bridge.fps()))
            self.tl.add(dlg.text(), default, after=after)
            self.edited.emit("Kimodo timeline: add segment"); self.update()

    def split_at(self, i, frame):
        st = self.tl.starts(self.start)[i]
        left = frame - st
        seg = self.tl.segments[i]
        if 0 < left < seg.frames:
            right = seg.frames - left
            self.tl.resize(i, left); self.tl.add(seg.prompt, right, after=i)
            self.edited.emit("Kimodo timeline: split segment"); self.update()

    def remove_segment(self, i):
        self.tl.remove(i); self.tl.clamp_keys(self.start)
        self.edited.emit("Kimodo timeline: delete segment"); self.update()

    def add_key(self, track, frame):
        end = self.start + self.tl.total_frames - 1
        self.tl.add_key(track, max(self.start, min(frame, end)))
        self.edited.emit("Kimodo timeline: add key"); self.update()

    def remove_key(self, track, frame):
        self.tl.remove_key(track, frame); self.edited.emit("Kimodo timeline: delete key"); self.update()

    def clear_track(self, track):
        self.tl.tracks[track] = []; self.edited.emit("Kimodo timeline: clear track"); self.update()


class TimelineWidget(QtWidgets.QWidget):
    """The panel: header (node), canvas, footer (transition, total, Fit, Generate, Status)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.node = None
        self._last_json = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6); lay.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        self.node_label = QtWidgets.QLabel("Select a Kimodo Motion node")
        self.node_label.setStyleSheet("font-weight: bold")
        head.addWidget(self.node_label); head.addStretch(1)
        self.detach_btn = QtWidgets.QPushButton("Detach timeline")
        self.detach_btn.setToolTip("Drop the timeline and go back to the node's Prompt + Duration. Undoable.")
        self.detach_btn.clicked.connect(self._detach)
        head.addWidget(self.detach_btn)
        lay.addLayout(head)

        self.canvas = Canvas(self)
        self.canvas.setToolTip("Wheel: zoom · Middle-drag: pan · F: fit · Drag block edge: resize · Drag block: reorder · "
                               "Double-click: edit · Right-click: add / split / delete")
        self.canvas.edited.connect(self._write)
        self.canvas.frameRequested.connect(bridge.set_frame)
        lay.addWidget(self.canvas, 1)

        foot = QtWidgets.QHBoxLayout()
        foot.addWidget(QtWidgets.QLabel("Transition"))
        self.transition = QtWidgets.QSpinBox(); self.transition.setRange(1, 60); self.transition.setSuffix(" samples")
        self.transition.setToolTip("Kimodo blends consecutive segments over this many clip samples (30 fps) at each boundary.")
        self.transition.valueChanged.connect(self._transition_changed)
        foot.addWidget(self.transition)
        foot.addSpacing(16)
        self.total_label = QtWidgets.QLabel("")
        foot.addWidget(self.total_label)
        foot.addStretch(1)
        self.fit_btn = QtWidgets.QPushButton("Fit"); self.fit_btn.setToolTip("Show the whole timeline (F)")
        self.fit_btn.clicked.connect(self.canvas.fit)
        foot.addWidget(self.fit_btn)
        foot.addSpacing(12)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #9a9a9a")
        foot.addWidget(self.status_label)
        self.cancel_btn = QtWidgets.QPushButton("Cancel"); self.cancel_btn.clicked.connect(lambda: self.node and bridge.cancel(self.node))
        foot.addWidget(self.cancel_btn)
        self.gen_btn = QtWidgets.QPushButton("Generate"); self.gen_btn.setDefault(True)
        self.gen_btn.setStyleSheet("font-weight: bold; padding: 4px 18px")
        self.gen_btn.clicked.connect(self._generate)
        foot.addWidget(self.gen_btn)
        lay.addLayout(foot)

        self._timer = QtCore.QTimer(self); self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick); self._timer.start()
        self._tick()

    # ── binding ──────────────────────────────────────────────────────────────
    def _tick(self):
        node = bridge.find_node()
        try:
            if node is not None and self.node is not None and node.path() == self.node.path():
                # external change (undo, manual parm edit)? reload, keep the view
                raw = node.parm("timeline_json").eval()
                if raw != self._last_json:
                    self._load(node, keep_view=True)
            elif node is not None:
                self._load(node)
            elif self.node is not None:
                try:
                    self.node.path()          # raises if the node was deleted
                except hou.ObjectWasDeleted:
                    self.node = None
        except Exception:
            self.node = None
        enabled = self.node is not None
        for w in (self.canvas, self.transition, self.gen_btn, self.cancel_btn, self.detach_btn, self.fit_btn):
            w.setEnabled(enabled)
        if not enabled:
            self.node_label.setText("Select a Kimodo Motion node"); self.node_label.setStyleSheet("font-weight: bold")
            self.status_label.setText(""); return
        self.canvas.playhead = bridge.current_frame()
        self.canvas.start = bridge.start_frame(self.node)
        self.canvas.update()
        self.status_label.setText(bridge.status(self.node))
        self.detach_btn.setVisible(bridge.has_timeline(self.node))
        # Pose keys need a posed rig on input 1; say so before Generate has to refuse.
        has_keys = any(self.canvas.tl.tracks.get(t) for t in self.canvas.tl.tracks)
        if has_keys and self.node.input(1) is None:
            self.node_label.setText(f"{self.node.path()}   ⚠ pose keys need a posed skeleton on input 1 (Create Pose Rig)")
            self.node_label.setStyleSheet("font-weight: bold; color: #e0a030")
        else:
            self.node_label.setText(self.node.path())
            self.node_label.setStyleSheet("font-weight: bold")

    def _load(self, node, keep_view=False):
        self.node = node
        self.canvas.tl = bridge.load(node)
        self.canvas.start = bridge.start_frame(node)
        self._last_json = node.parm("timeline_json").eval()
        self.transition.blockSignals(True); self.transition.setValue(self.canvas.tl.transition_frames); self.transition.blockSignals(False)
        self._refresh_total()
        if not keep_view:
            self.canvas.fit()

    def _refresh_total(self):
        tl = self.canvas.tl
        self.total_label.setText(f"Total {tl.total_frames} frames · {tl.total_frames / bridge.fps():.2f} s · {len(tl.segments)} segment(s)")

    def _write(self, label="Kimodo timeline edit"):
        if self.node is None:
            return
        bridge.save(self.node, self.canvas.tl, label)
        self._last_json = self.node.parm("timeline_json").eval()
        self._refresh_total()

    def _transition_changed(self, v):
        self.canvas.tl.transition_frames = int(v); self._write("Kimodo timeline: transition")
        self.canvas.update()

    def _detach(self):
        if self.node is not None:
            bridge.detach(self.node); self._load(self.node)

    def _generate(self):
        if self.node is None:
            return
        if not self.canvas.tl.segments:
            self.status_label.setText("Add at least one segment"); return
        self._write("Kimodo timeline: generate")
        bridge.generate(self.node)
