"""Kimodo Timeline: PySide6 view over model.Timeline, bound to a Kimodo Motion node.

View controls follow the usual DCC timeline conventions:
  wheel            zoom around the cursor
  middle-drag      pan
  F / Home / Fit   fit the whole timeline
  left-drag ruler  scrub the Houdini frame
"""
from __future__ import annotations

import html

import hou
from PySide6 import QtCore, QtGui, QtWidgets

from . import bridge
from .model import MIN_FRAMES, TRACKS, TRACK_LABELS, Timeline

# -- layout constants (pixels) --------------------------------------------------
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
def text_on(bg):
    """Black or white body text, whichever stays readable on this block colour.
    The amber and green segment colours are too light to carry white text."""
    lum = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
    if lum > 0.55:
        return QtGui.QColor(18, 18, 18), QtGui.QColor(18, 18, 18, 195)
    return QtGui.QColor(255, 255, 255), QtGui.QColor(255, 255, 255, 200)


BG = QtGui.QColor("#2b2b2b"); ROW_BG = QtGui.QColor("#333333"); GRID = QtGui.QColor("#454545")
TEXT = QtGui.QColor("#e6e6e6"); DIM = QtGui.QColor("#9a9a9a"); PLAYHEAD = QtGui.QColor("#ff5252"); HIPMARK = QtGui.QColor("#6f7a8a")
WHITE = QtGui.QColor("#ffffff"); OUTSIDE = QtGui.QColor(0, 0, 0, 60)


class PromptDialog(QtWidgets.QDialog):
    """Prompt plus duration. Frames and seconds are two views of one value, kept in sync."""

    def __init__(self, text: str, frames: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Segment")
        self.resize(480, 200)
        lay = QtWidgets.QVBoxLayout(self)
        self.edit = QtWidgets.QPlainTextEdit(text)
        self.edit.setPlaceholderText("What the character does, in English\u2026")
        lay.addWidget(self.edit)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Frames"))
        self.frames_box = QtWidgets.QSpinBox()
        self.frames_box.setRange(MIN_FRAMES, 100000)
        self.frames_box.setValue(max(MIN_FRAMES, int(frames)))
        self.frames_box.setToolTip("Segment length in Houdini frames.")
        self.frames_box.valueChanged.connect(self._frames_changed)
        row.addWidget(self.frames_box)
        row.addSpacing(16)
        row.addWidget(QtWidgets.QLabel("Seconds"))
        self.secs_box = QtWidgets.QDoubleSpinBox()
        self.secs_box.setDecimals(2); self.secs_box.setSingleStep(0.25)
        self.secs_box.setRange(MIN_FRAMES / max(bridge.fps(), 1.0), 100000.0)
        self.secs_box.setToolTip("The same length in seconds, at the scene FPS.")
        self.secs_box.valueChanged.connect(self._secs_changed)
        row.addWidget(self.secs_box)
        row.addStretch(1)
        lay.addLayout(row)
        self._frames_changed(self.frames_box.value())

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.edit.setFocus()

    def _frames_changed(self, v):
        self.secs_box.blockSignals(True)
        self.secs_box.setValue(v / max(bridge.fps(), 1.0))
        self.secs_box.blockSignals(False)

    def _secs_changed(self, v):
        self.frames_box.blockSignals(True)
        self.frames_box.setValue(max(MIN_FRAMES, int(round(v * bridge.fps()))))
        self.frames_box.blockSignals(False)

    def text(self) -> str:
        return self.edit.toPlainText().strip()

    def frames(self) -> int:
        return int(self.frames_box.value())


class Canvas(QtWidgets.QWidget):
    """Ruler + prompt row + one row per track. Emits `edited` after a model change that
    should be written to the node, `frameRequested` when the ruler is scrubbed."""
    edited = QtCore.Signal(str)          # undo label
    frameRequested = QtCore.Signal(int)
    regenRequested = QtCore.Signal(int, bool)   # segment index, and whether to run to the end

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tl = Timeline()
        self.start = 1
        self.playhead = 1
        self.hip_range = (1, 1)   # Houdini's playbar range, drawn as bounds
        # view state: frame at the left edge of the work zone and pixels per frame
        self._view_start = 0.0
        self._ppf = 10.0
        self._fitted = False
        # interaction state
        self._mode = None                 # None | "resize" | "move" | "key" | "scrub" | "pan"
        self._hover = -1                  # segment under the cursor, -1 for none
        self._base_tip = None             # the controls hint, restored when not over a segment
        self._idx = -1                    # segment index being edited
        self._track = None                # track name for key drags
        self._key = None                  # current key frame for key drags
        self._drop = None                 # drop index while moving a segment
        self._pan_x = 0.0
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMinimumHeight(RULER_H + PROMPT_H + TRACK_H * len(TRACKS) + 8)
        self.setContextMenuPolicy(QtCore.Qt.DefaultContextMenu)

    # -- view -----------------------------------------------------------------
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

    # -- geometry helpers -----------------------------------------------------
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
            out.append(QtCore.QRectF(x0, RULER_H, x1 - x0, PROMPT_H))
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

    # -- painting -------------------------------------------------------------
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

        # outside-the-clip shading, both ends
        x_start = self.x_of(self.start)
        if x_start > GUTTER:
            p.fillRect(QtCore.QRectF(GUTTER, RULER_H, x_start - GUTTER, h - RULER_H), OUTSIDE)
        x_end = self.x_of(self.start + self.tl.total_frames)
        if x_end < w - PAD_R:
            p.fillRect(QtCore.QRectF(x_end, RULER_H, (w - PAD_R) - x_end, h - RULER_H), OUTSIDE)

        # ruler + grid: tick every N frames so labels stay >= 48 px apart
        step = 1200
        for cand in (1, 2, 5, 10, 12, 24, 25, 30, 48, 50, 60, 100, 120, 240, 300, 600):
            if cand * self._ppf >= 48:
                step = cand; break
        fm = p.fontMetrics()
        ph_x = self.x_of(self.playhead)
        ph_w = max(30.0, fm.horizontalAdvance(str(self.playhead)) + 14)
        ph_badge = QtCore.QRectF(ph_x - ph_w / 2, 2, ph_w, RULER_H - 6)
        f0 = self.frame_at(GUTTER); f1 = self.frame_at(w - PAD_R)
        f = f0 - (f0 % step)
        while f <= f1:
            x = self.x_of(f)
            p.setPen(GRID); p.drawLine(QtCore.QPointF(x, RULER_H), QtCore.QPointF(x, h))
            lab = str(f)
            # a tick label peeking out from behind the playhead badge reads as a second number
            if not QtCore.QRectF(x + 3, 0, fm.horizontalAdvance(lab), RULER_H).intersects(ph_badge):
                p.setPen(TEXT); p.drawText(QtCore.QPointF(x + 3, RULER_H - 7), lab)
            f += step

        # HIP playbar bounds, so the scene range is readable against the clip
        for hf in self.hip_range:
            hx = self.x_of(hf)
            if GUTTER <= hx <= w - PAD_R:
                p.setPen(QtGui.QPen(HIPMARK, 1, QtCore.Qt.DashLine))
                p.drawLine(QtCore.QPointF(hx, RULER_H), QtCore.QPointF(hx, h))
        p.setPen(QtGui.QPen(QtGui.QColor(90, 90, 90), 1))
        p.drawLine(QtCore.QPointF(GUTTER, RULER_H - 0.5), QtCore.QPointF(w - PAD_R, RULER_H - 0.5))

        # segments
        fps = bridge.fps()
        rects = self._seg_rects()
        for i, r in enumerate(rects):
            col = SEG_COLORS[i % len(SEG_COLORS)]
            if self._mode == "move" and i == self._idx:
                col = QtGui.QColor(col); col.setAlpha(110)
            elif i == self._hover and self._mode is None:
                col = col.lighter(122)
            fg, fg_dim = text_on(col)
            rr = r.adjusted(1, 0, -1, 0)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(col)
            p.drawRoundedRect(rr, 5, 5)
            # edge colour follows the contrast rule, so it stays visible on light blocks
            edge = QtGui.QColor(fg); edge.setAlpha(55)
            p.setPen(QtGui.QPen(edge, 1)); p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(rr.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            seg = self.tl.segments[i]
            secs = seg.frames / fps
            txt = QtCore.QRectF(r.left() + 6, r.top() + 2, max(0, r.width() - 12), r.height() - 4)
            if txt.width() > 24:
                p.setPen(fg)
                p.drawText(txt, QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop | QtCore.Qt.TextSingleLine,
                           fm.elidedText(seg.prompt or "(empty prompt)", QtCore.Qt.ElideRight, int(txt.width())))
                p.setPen(fg_dim)
                p.drawText(txt, QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom, f"{seg.frames} f \u00b7 {secs:.2f} s")
            p.setPen(QtGui.QPen(fg, 2))
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

        # keys (and a hint on empty tracks)
        for t in TRACKS:
            y = self._track_y(t)
            col = TRACK_COLORS[t]
            if not self.tl.tracks.get(t) and t == TRACKS[0]:
                p.setPen(QtGui.QColor(120, 120, 120))
                p.drawText(QtCore.QPointF(GUTTER + 8, y + 4), "double-click or right-click a track to add a pose key")
            for k in self.tl.tracks.get(t, []):
                x = self.x_of(k)
                path = QtGui.QPainterPath()
                path.moveTo(x, y - KEY_R); path.lineTo(x + KEY_R, y); path.lineTo(x, y + KEY_R); path.lineTo(x - KEY_R, y); path.closeSubpath()
                p.setPen(QtGui.QPen(QtGui.QColor("#111111"), 1)); p.setBrush(col); p.drawPath(path)

        # playhead. Blocks and shading treat a frame as the cell [f, f+1), so the current
        # frame is filled across its whole cell; the line alone marked only its left edge,
        # which made the end-of-clip shading look a frame late.
        x = self.x_of(self.playhead)
        x_next = self.x_of(self.playhead + 1)
        p.fillRect(QtCore.QRectF(x, RULER_H, x_next - x, h - RULER_H), QtGui.QColor(255, 82, 82, 38))
        p.setPen(QtGui.QPen(PLAYHEAD, 2)); p.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, h))
        p.setBrush(PLAYHEAD); p.setPen(QtCore.Qt.NoPen)
        p.drawRoundedRect(ph_badge, 3, 3)
        p.setPen(WHITE); p.drawText(ph_badge, QtCore.Qt.AlignCenter, str(self.playhead))
        p.end()

    # -- mouse ----------------------------------------------------------------
    def mousePressEvent(self, ev):
        pos = ev.position()
        if ev.button() == QtCore.Qt.MiddleButton:
            self._mode = "pan"; self._pan_x = pos.x(); self.setCursor(QtCore.Qt.ClosedHandCursor); return
        if ev.button() != QtCore.Qt.LeftButton:
            return super().mousePressEvent(ev)
        row, track = self._row_of(pos.y())
        if row == "ruler":
            self._mode = "scrub"; self._scrub_to(pos.x()); return
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
            self._scrub_to(pos.x()); return
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
        # hover: cursor, block highlight, and the full prompt in a tooltip
        row, track = self._row_of(pos.y())
        cur = QtCore.Qt.ArrowCursor
        hover = -1
        if row == "prompt":
            i, r = self._seg_at(pos)
            if i >= 0:
                hover = i
                cur = QtCore.Qt.SizeHorCursor if pos.x() >= r.right() - EDGE_GRAB - 2 else QtCore.Qt.OpenHandCursor
        elif row == "track" and self._key_at(track, pos.x()) is not None:
            cur = QtCore.Qt.SizeHorCursor
        self.setCursor(cur)
        if hover != self._hover:
            self._hover = hover
            self._set_tip(self._seg_tip(hover) if hover >= 0 else None)
            self.update()

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self._set_tip(None)
            self.update()
        super().leaveEvent(ev)

    def _set_tip(self, text):
        """Segment tooltip while hovering one, the controls hint otherwise."""
        if self._base_tip is None:
            self._base_tip = self.toolTip()
        self.setToolTip(text or self._base_tip)

    def _seg_tip(self, i):
        """The whole prompt, which the block itself has to elide."""
        seg = self.tl.segments[i]
        st = self.tl.starts(self.start)[i]
        safe = html.escape(seg.prompt or "(empty prompt)")
        return (f"<b>Segment {i + 1} of {len(self.tl.segments)}</b>"
                f"<br>frames {st}-{st + seg.frames - 1}"
                f" &nbsp;({seg.frames} f, {seg.frames / bridge.fps():.2f} s)"
                f"<br><br>{safe}")

    def mouseReleaseEvent(self, ev):
        mode, self._mode = self._mode, None
        if mode == "pan":
            self.setCursor(QtCore.Qt.ArrowCursor)
        elif mode == "resize":
            self.tl.clamp_keys(self.start); self._commit("resize segment")
        elif mode == "move":
            drop = self._drop if self._drop is not None else self._idx
            if drop > self._idx:
                drop -= 1                    # removing src shifts later indices left
            if drop != self._idx:
                self.tl.move(self._idx, drop); self._commit("reorder segments")
            self._drop = None
        elif mode == "key":
            self._commit("move key")
        self.update()

    def _commit(self, verb):
        """One model edit written to the node, as one undo step."""
        self.edited.emit("Kimodo timeline: " + verb)
        self.update()

    def mouseDoubleClickEvent(self, ev):
        pos = ev.position()
        row, track = self._row_of(pos.y())
        if row == "prompt":
            i, _ = self._seg_at(pos)
            if i >= 0:
                self._mode = None
                self._later(lambda: self.edit_prompt(i))
        elif row == "track" and pos.x() >= GUTTER:
            # double-click on a track adds a key at that frame (or removes the one under the cursor)
            self._mode = None
            k = self._key_at(track, pos.x())
            if k is None:
                self.add_key(track, self.frame_at(pos.x()))
            else:
                self.remove_key(track, k)
        elif row == "ruler":
            self.fit()

    def contextMenuEvent(self, ev):
        pos = QtCore.QPointF(ev.pos())
        row, track = self._row_of(pos.y())
        menu = QtWidgets.QMenu(self)
        if row == "prompt":
            i, _ = self._seg_at(pos)
            if i >= 0:
                menu.addAction("Edit segment\u2026", lambda: self._later(lambda: self.edit_prompt(i)))
                menu.addAction("Add segment after", lambda: self._later(lambda: self.add_segment(after=i)))
                menu.addAction("Split at playhead", lambda: self.split_at(i, self.playhead))
                menu.addSeparator()
                one = menu.addAction("Regenerate this sequence\u2026",
                                     lambda: self.regenRequested.emit(i, False))
                rest = menu.addAction("Regenerate from here to the end\u2026",
                                      lambda: self.regenRequested.emit(i, True))
                for act in (one, rest):
                    act.setEnabled(i > 0)
                    if i == 0:
                        act.setToolTip("The first sequence has no earlier motion to "
                                       "continue from; use Generate.")
                menu.addSeparator()
                menu.addAction("Delete segment", lambda: self.remove_segment(i))
            else:
                menu.addAction("Add segment at end", lambda: self._later(self.add_segment))
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
        menu.deleteLater()          # one menu was leaking per right-click

    def _scrub_to(self, x):
        """Move our own playhead and repaint straight away, then ask Houdini to follow.
        Waiting for the 400 ms tick to pick the frame up is what made dragging feel dead."""
        f = self.frame_at(x)
        if f != self.playhead:
            self.playhead = f
            self.update()
        self.frameRequested.emit(f)

    # -- model edits (each ends in one undoable write) ------------------------
    def _later(self, fn):
        """Run fn from the event loop rather than from inside the handler we are in.

        Anything that opens a nested event loop (a modal dialog) or hands control to
        Houdini (a progress dialog, a forced cook) must not do it during Qt event
        dispatch. Houdini blocks mouse input to its own panes while it believes a modal
        operation is running, and entering that state from inside a mouse handler, or
        from inside QMenu.exec, leaves the block in place after the dialog has gone: the
        panel keeps working because it is plain Qt, keyboard accelerators keep working,
        and every pane of Houdini ignores the mouse.
        """
        QtCore.QTimer.singleShot(0, fn)

    def edit_prompt(self, i):
        seg = self.tl.segments[i]
        dlg = PromptDialog(seg.prompt, seg.frames, hou.qt.mainWindow())
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.tl.set_prompt(i, dlg.text())
            if dlg.frames() != seg.frames:
                self.tl.resize(i, dlg.frames()); self.tl.clamp_keys(self.start)
            self._commit("edit segment")

    def add_segment(self, after=None):
        default = self.tl.segments[after].frames if after is not None and self.tl.segments else int(round(3 * bridge.fps()))
        dlg = PromptDialog("", default, hou.qt.mainWindow())
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.tl.add(dlg.text(), dlg.frames(), after=after)
            self._commit("add segment")

    def split_at(self, i, frame):
        st = self.tl.starts(self.start)[i]
        left = frame - st
        seg = self.tl.segments[i]
        if 0 < left < seg.frames:
            right = seg.frames - left
            self.tl.resize(i, left); self.tl.add(seg.prompt, right, after=i)
            self._commit("split segment")

    def remove_segment(self, i):
        self.tl.remove(i); self.tl.clamp_keys(self.start)
        self._commit("delete segment")

    def add_key(self, track, frame):
        end = self.start + self.tl.total_frames - 1
        self.tl.add_key(track, max(self.start, min(frame, end)))
        self._commit("add key")

    def remove_key(self, track, frame):
        self.tl.remove_key(track, frame); self._commit("delete key")

    def clear_track(self, track):
        self.tl.tracks[track] = []; self._commit("clear track")


class TimelineWidget(QtWidgets.QWidget):
    """The panel: header (node), canvas, footer (transition, total, Fit, Generate, Status)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.node = None
        self._last_json = None
        self._last_selected = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6); lay.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        self.node_combo = QtWidgets.QComboBox()
        self.node_combo.setMinimumWidth(240)
        self.node_combo.setToolTip("Kimodo Motion nodes in this scene. Pick one to edit its timeline; "
                                   "selecting a node in the network editor also switches here.")
        self.node_combo.activated.connect(self._combo_picked)
        head.addWidget(self.node_combo)
        self.warn_label = QtWidgets.QLabel("")
        self.warn_label.setStyleSheet("font-weight: bold; color: #e0a030")
        head.addWidget(self.warn_label); head.addStretch(1)
        lay.addLayout(head)

        self.canvas = Canvas(self)
        self.canvas.setToolTip("Wheel: zoom \u00b7 Middle-drag: pan \u00b7 F: fit \u00b7 Drag block edge: resize \u00b7 Drag block: reorder \u00b7 "
                               "Double-click: edit prompt + length \u00b7 Right-click: add / split / delete")
        self.canvas.edited.connect(self._write)
        # Queued, not direct. Run synchronously, both of these re-enter Houdini from
        # inside a Qt event handler: set_frame calls hou.ui.triggerUpdate from
        # Canvas.mouseMoveEvent on every scrub step, and _regen opens a modal progress
        # dialog while the context menu's own exec() loop is still on the stack.
        # Re-entering Houdini's UI cycle mid-dispatch leaves it routing mouse input to
        # whichever pane was active at re-entry, so every pane except this one stops
        # answering the mouse until something forces a pane focus change. A queued
        # connection runs the slot from the event loop once the handler has returned.
        # Scrubbing still reads as immediate because _scrub_to moves the canvas's own
        # playhead before it emits.
        self.canvas.frameRequested.connect(bridge.set_frame, QtCore.Qt.QueuedConnection)
        self.canvas.regenRequested.connect(self._regen, QtCore.Qt.QueuedConnection)
        lay.addWidget(self.canvas, 1)

        foot = QtWidgets.QHBoxLayout()
        foot.addWidget(QtWidgets.QLabel("Transition"))
        self.transition = QtWidgets.QSpinBox(); self.transition.setRange(1, 60); self.transition.setSuffix(" samples")
        self.transition.setToolTip("Kimodo blends consecutive segments over this many clip samples (30 fps) at each boundary.")
        self.transition.valueChanged.connect(self._transition_changed)
        self.transition.editingFinished.connect(lambda: self._write("Kimodo timeline: transition"))
        foot.addWidget(self.transition)
        foot.addSpacing(16)
        self.total_label = QtWidgets.QLabel("")
        self.total_label.setTextFormat(QtCore.Qt.RichText)
        self.total_label.setToolTip("First and last scene frame this timeline occupies, "
                                    "from the node's Start Frame.")
        foot.addWidget(self.total_label)
        foot.addStretch(1)
        self.key_track = QtWidgets.QComboBox()
        for t in TRACKS:
            self.key_track.addItem(TRACK_LABELS[t], t)
        self.key_track.setToolTip("Track for Add Key")
        foot.addWidget(self.key_track)
        self.key_btn = QtWidgets.QPushButton("Add Key at Playhead")
        self.key_btn.setToolTip("Add a pose key on the chosen track at the current Houdini frame. "
                                "Also: double-click a track row, or right-click it.")
        self.key_btn.clicked.connect(lambda: self.canvas.add_key(self.key_track.currentData(), self.canvas.playhead))
        foot.addWidget(self.key_btn)
        foot.addSpacing(12)
        self.fit_btn = QtWidgets.QPushButton("Fit"); self.fit_btn.setToolTip("Show the whole timeline (F)")
        self.fit_btn.clicked.connect(self.canvas.fit)
        foot.addWidget(self.fit_btn)
        foot.addSpacing(12)
        self.progress = QtWidgets.QProgressBar(); self.progress.setRange(0, 1000); self.progress.setTextVisible(True)
        self.progress.setFixedWidth(160); self.progress.setFormat("%p%"); self.progress.hide()
        foot.addWidget(self.progress)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #9a9a9a")
        foot.addWidget(self.status_label)
        self.cancel_btn = QtWidgets.QPushButton("Cancel"); self.cancel_btn.clicked.connect(lambda: self.node and bridge.cancel(self.node))
        foot.addWidget(self.cancel_btn)
        self.gen_btn = QtWidgets.QPushButton("Generate"); self.gen_btn.setDefault(True)
        # No stylesheet here: any stylesheet on a QPushButton hands rendering to
        # QStyleSheetStyle, which draws the CSS box model and drops Houdini's native
        # button background. Get bold and width the native way so the chrome survives.
        _f = self.gen_btn.font(); _f.setBold(True); self.gen_btn.setFont(_f)
        self.gen_btn.setMinimumWidth(110)
        self.gen_btn.clicked.connect(self._generate)
        foot.addWidget(self.gen_btn)
        lay.addLayout(foot)

        self._timer = QtCore.QTimer(self); self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick); self._timer.start()
        # The 400 ms tick is too coarse to follow playback, and running the whole tick at
        # frame rate would be wasteful. A second timer moves only the playhead.
        self._play_timer = QtCore.QTimer(self); self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._sync_playhead); self._play_timer.start()
        self._tick()

    def _sync_playhead(self):
        """Follow the Houdini frame at ~30 fps. One HOM call, repaint only on a change."""
        if self.node is None or not self.isVisible():
            return
        try:
            f = bridge.current_frame()
        except Exception:
            return
        if f != self.canvas.playhead:
            self.canvas.playhead = f
            self.canvas.update()

    # -- binding --------------------------------------------------------------
    def _sync_combo(self):
        """Refresh the node list without disturbing the current pick."""
        paths = [n.path() for n in bridge.all_nodes()]
        if paths == [self.node_combo.itemData(i) for i in range(self.node_combo.count())]:
            return
        self.node_combo.blockSignals(True)
        self.node_combo.clear()
        for p in paths:
            self.node_combo.addItem(p, p)
        self.node_combo.blockSignals(False)

    def _combo_picked(self, index):
        node = bridge.node_at(self.node_combo.itemData(index))
        if node is not None and (self.node is None or node.path() != self.node.path()):
            self._load(node)

    def _tick(self):
        if not self.isVisible():      # hidden pane tab: nothing to poll for
            return
        try:
            self._sync_combo()
            # Only a *new* network-editor selection switches the panel, so a pick made
            # in the combo stands and either Kimodo node can be driven from this panel.
            sel = bridge.find_node()
            sel_path = sel.path() if sel is not None else None
            if sel_path is not None and sel_path != self._last_selected:
                self._load(sel)
            self._last_selected = sel_path
            if self.node is None and self.node_combo.count():
                first = bridge.node_at(self.node_combo.itemData(0))
                if first is not None:
                    self._load(first)
            if self.node is not None:
                self.node.path()              # raises if the node was deleted
                raw = self.node.parm("timeline_json").eval()
                if raw != self._last_json:    # external change (undo, manual parm edit)
                    self._load(self.node, keep_view=True)
        except hou.ObjectWasDeleted:
            self.node = None
        enabled = self.node is not None
        for w in (self.canvas, self.transition, self.gen_btn, self.cancel_btn, self.fit_btn, self.key_btn, self.key_track):
            w.setEnabled(enabled)
        if not enabled:
            self.warn_label.setText("")
            self.status_label.setText(""); return
        new_start = bridge.start_frame(self.node)
        new_hip = bridge.hip_frame_range()
        if new_start != self.canvas.start or new_hip != self.canvas.hip_range:
            self.canvas.start = new_start       # Start Frame or the HIP range moved
            self.canvas.hip_range = new_hip
            self._refresh_total()
            self.canvas.update()
        st = bridge.status(self.node)
        self.status_label.setText(st)
        prog = bridge.progress(self.node)
        self.progress.setVisible(prog is not None)
        if prog is not None:
            self.progress.setValue(int(prog * 1000))
        i = self.node_combo.findData(self.node.path())
        if i >= 0 and i != self.node_combo.currentIndex():
            self.node_combo.blockSignals(True)
            self.node_combo.setCurrentIndex(i)
            self.node_combo.blockSignals(False)
        # Pose keys need a posed rig on input 1; say so before Generate has to refuse.
        has_keys = any(self.canvas.tl.tracks.values())
        self.warn_label.setText(
            "\u26a0 pose keys need a posed skeleton on input 1 (Create Pose Rig)"
            if has_keys and self.node.input(1) is None else "")

    def _load(self, node, keep_view=False):
        self.node = node
        self.canvas.tl = bridge.load(node)
        self.canvas.start = bridge.start_frame(node)
        self._last_json = node.parm("timeline_json").eval()
        self.transition.blockSignals(True); self.transition.setValue(self.canvas.tl.transition_frames); self.transition.blockSignals(False)
        self._refresh_total()
        if keep_view:
            self.canvas.update()
        else:
            self.canvas.fit()

    def _refresh_total(self):
        tl = self.canvas.tl
        first = self.canvas.start
        last = first + tl.total_frames - 1 if tl.total_frames else first
        h0, h1 = self.canvas.hip_range
        fits = h0 <= first and h1 >= last
        hip = (f"HIP <b>{h0}</b>-<b>{h1}</b>" if fits else
               f"<span style='color:#e0a030'>HIP <b>{h0}</b>-<b>{h1}</b>, clip does not fit</span>")
        self.total_label.setText(
            f"Clip <b>{first}</b>-<b>{last}</b>   {tl.total_frames} f \u00b7 "
            f"{tl.total_frames / bridge.fps():.2f} s \u00b7 {len(tl.segments)} segment(s)"
            f"     <span style='color:#9a9a9a'>{hip}</span>")

    def _write(self, label="Kimodo timeline edit"):
        if self.node is None:
            return
        bridge.save(self.node, self.canvas.tl, label)
        self._last_json = self.node.parm("timeline_json").eval()
        self._refresh_total()

    def _transition_changed(self, v):
        self.canvas.tl.transition_frames = int(v)
        self.canvas.update()

    def _regen(self, index, to_end=False):
        """Re-roll a sequence, keeping what came before and, unless `to_end`, what comes
        after. Imported lazily: it needs numpy, and the rest of the panel does not."""
        if self.node is None:
            return
        self._write("Kimodo timeline: regenerate")
        try:
            from . import regen
        except ImportError as e:
            self.status_label.setText("regen unavailable: %s" % e)
            return
        try:
            msg = regen.regenerate(self.node, index, to_end=to_end)
        except hou.OperationInterrupted:
            self.status_label.setText("Cancelled")
        except Exception as e:
            self.status_label.setText(str(e))
            if hou.isUIAvailable():
                hou.ui.setStatusMessage("Kimodo: %s" % e, severity=hou.severityType.Error)
        else:
            self.status_label.setText(msg)

    def _generate(self):
        if self.node is None:
            return
        if not self.canvas.tl.segments:
            self.status_label.setText("Add at least one segment"); return
        self._write("Kimodo timeline: generate")
        bridge.generate(self.node)
