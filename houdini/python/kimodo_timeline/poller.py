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

POLL_S = 1.0          # seconds between server queries; the callback itself runs far more often
MAX_FAILS = 3


class JobWatcher:
    """Polls one job id and drives the node's parms. `on_done(data)` finishes the work."""

    def __init__(self, node, url: str, job_id: str, label: str, on_done, poll_s: float = POLL_S):
        self.node, self.url, self.job_id = node, url.rstrip("/"), job_id
        self.label, self.on_done, self.poll_s = label, on_done, poll_s
        self._due = 0.0
        self._fails = 0

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        # hou.ui only exists in a UI session, and there is no event loop to poll from
        # without one. Say so rather than dying on an AttributeError.
        if not hou.isUIAvailable():
            raise hou.OperationFailed(
                "Kimodo polls the job from Houdini's event loop, which needs a UI session.")
        hou.ui.addEventLoopCallback(self._tick)

    def stop(self) -> None:
        try:
            hou.ui.removeEventLoopCallback(self._tick)
        except hou.OperationFailed:
            pass                      # already removed, or never added

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
                self.node.parm("status").set(
                    "%s %d%%" % (self.label, int(prog * 100))
                    + (" · %s" % phase if phase else "") + suffix)
            else:
                self.node.parm("status").set("%s...%s" % (self.label, suffix))
