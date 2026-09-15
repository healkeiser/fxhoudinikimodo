"""Watch a Kimodo job from Houdini's event loop, without blocking anything.

This is how SideFX's own code does repeated work: hou.ui.addEventLoopCallback runs the
callback once per event-loop iteration, and hdefereval is built on it. Nothing sleeps on
the main thread, no nested event loop is entered, no modal dialog is opened and no
operation scope exists to leak, which is what every earlier version of this got wrong.

State goes on the node, in `status` and `progress`, which the Timeline panel's own
progress bar already renders and which show under the node in the network editor.
"""
from __future__ import annotations

import time

import hou
import requests
from .qt import QtWidgets

POLL_S = 1.0          # seconds between server queries; the callback itself runs far more often
MAX_FAILS = 3


class JobDialog(QtWidgets.QDialog):
    """Non-modal progress for a running job, driven by JobWatcher.

    show(), never exec(). A modal nested event loop is what locked Houdini's panes out
    of the mouse, and none of it was ever needed: this is updated from the watcher's
    event-loop callback, so Houdini stays fully interactive while it is up, and it
    appears at once rather than waiting out a minimumDuration or an interrupt threshold.

    Cancel only raises a flag. The watcher does the actual POST on its next tick, so all
    I/O stays in one place and no button handler blocks.
    """

    def __init__(self, title: str):
        super().__init__(hou.qt.mainWindow())
        self.cancel_requested = False
        self.setWindowTitle(title)
        # hou.qt.styleSheet is what SideFX's own houpythonportion/qt/Dialog.py calls,
        # and what fxgui's fxdcc.get_houdini_stylesheet returns. hou.ui.qtStyleSheet is
        # the same string.
        self.setStyleSheet(hou.qt.styleSheet())
        lay = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel("Queued")
        lay.addWidget(self.label)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        lay.addWidget(self.bar)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.btn = QtWidgets.QPushButton("Cancel")
        self.btn.setToolTip("Cancel the job on the server." + "\n" +
                            "Closing this window only hides it: the job keeps running and "
                            "the Kimodo Timeline panel keeps showing its progress.")
        self.btn.clicked.connect(self._request_cancel)
        row.addWidget(self.btn)
        lay.addLayout(row)
        self.resize(430, 120)

    def _request_cancel(self) -> None:
        self.cancel_requested = True
        self.btn.setEnabled(False)
        self.label.setText("Cancelling\u2026")

    def report(self, fraction: float, text: str) -> None:
        self.label.setText(text)
        self.bar.setValue(max(0, min(1000, int(fraction * 1000))))


class JobWatcher:
    """Polls one job id and drives the node's parms. `on_done(data)` finishes the work."""

    def __init__(self, node, url: str, job_id: str, label: str, on_done, poll_s: float = POLL_S):
        self.node, self.url, self.job_id = node, url.rstrip("/"), job_id
        self.label, self.on_done, self.poll_s = label, on_done, poll_s
        self.dlg = None
        self._due = 0.0
        self._fails = 0

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        # hou.ui only exists in a UI session, and there is no event loop to poll from
        # without one. Say so rather than dying on an AttributeError.
        if not hou.isUIAvailable():
            raise hou.OperationFailed(
                "Kimodo polls the job from Houdini's event loop, which needs a UI session.")
        self.dlg = JobDialog("Kimodo")
        self.dlg.report(0.0, self.label)
        self.dlg.show()               # not exec(): nothing blocks, nothing nests
        hou.ui.addEventLoopCallback(self._tick)

    def stop(self) -> None:
        try:
            hou.ui.removeEventLoopCallback(self._tick)
        except hou.OperationFailed:
            pass                      # already removed, or never added
        if self.dlg is not None:
            self.dlg.close()
            self.dlg.deleteLater()    # parented to the main window, so it would linger
            self.dlg = None

    def fail(self, msg) -> None:
        self.stop()
        self.node.parm("last_error").set(str(msg))
        self.node.parm("status").set("Error: %s" % msg)
        self.node.parm("job_id").set("")
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Kimodo: %s" % msg, severity=hou.severityType.Error)

    # -- the callback ---------------------------------------------------------
    def _tick(self) -> None:
        now = time.monotonic()
        if now < self._due:           # runs every event-loop iteration, so leave fast
            return
        self._due = now + self.poll_s
        try:
            self._poll_once()
        except hou.ObjectWasDeleted:
            self.stop()               # node went away mid-job
        except Exception as e:        # never let a raising callback spin in the event loop
            self.fail(e)

    def _poll_once(self) -> None:
        if self.node.parm("job_id").eval() != self.job_id:
            self.stop()               # a newer Generate replaced us
            return
        if self.dlg is not None and self.dlg.cancel_requested:
            requests.post("%s/jobs/%s/cancel" % (self.url, self.job_id), timeout=10)
            self.node.parm("status").set("Cancelling...")
            self.dlg.cancel_requested = False   # let the server report it back as cancelled
            return
        try:
            r = requests.get("%s/jobs/%s" % (self.url, self.job_id), timeout=10)
            if r.status_code == 404:
                self.fail("Job lost (server restarted?)")
                return
            r.raise_for_status()
            data = r.json()
            self._fails = 0
        except requests.RequestException as e:
            self._fails += 1
            self.node.parm("status").set("Poll error (%d/%d): %s" % (self._fails, MAX_FAILS, e))
            if self._fails >= MAX_FAILS:
                self.fail("Lost contact with the server while polling: %s" % e)
            return

        status = data["status"]
        elapsed = data.get("elapsed")
        suffix = " (%ds)" % int(elapsed) if elapsed else ""
        if status == "done":
            self.stop()
            self.on_done(data, suffix)
        elif status == "failed":
            self.fail("Generation failed: %s" % (data.get("error") or "no detail from server"))
        elif status == "cancelled":
            self.stop()
            self.node.parm("status").set("Cancelled")
            self.node.parm("job_id").set("")
        else:
            prog, phase = data.get("progress"), data.get("phase")
            if prog is not None:
                self.node.parm("progress").set(float(prog))
                text = ("%s %d%%" % (self.label, int(prog * 100))
                        + (" \u00b7 %s" % phase if phase else "") + suffix)
                self.node.parm("status").set(text)
                if self.dlg is not None:
                    self.dlg.report(float(prog), text)
            else:
                text = "%s...%s" % (self.label, suffix)
                self.node.parm("status").set(text)
                if self.dlg is not None:
                    self.dlg.report(0.0, text)
