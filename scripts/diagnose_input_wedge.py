"""Capture why Houdini's panes stopped answering the mouse.

Symptom: the Kimodo Timeline panel still responds, keyboard shortcuts still work, and
every Houdini pane ignores the mouse until a restart.

Run this in Houdini's Python Shell. Save a baseline while things work, then run it again
the moment it wedges; the second run prints the diff:

    exec(open("scripts/diagnose_input_wedge.py").read())
    baseline()      # while Houdini is healthy
    report()        # when it wedges

Houdini's UI is Qt, so the cause is Qt state. The reading most likely to matter is
WA_Disabled: Qt implements modality by disabling the widgets a modal dialog blocks and
clearing the flag on teardown. A teardown that misses one leaves a widget permanently
disabled with no modal widget left to explain it, which matches the symptom exactly.

Known dead ends, so nobody re-treads them:
  - hou.updateProgressAndCheckForInterrupt() never raises in this build, inside or
    outside a real operation. It is not a detector.
  - activeModalWidget, activePopupWidget, mouseGrabber, keyboardGrabber, overrideCursor
    and QGuiApplication.modalWindow() all read clean during a wedge.
  - QDialog.exec() on its own does not cause it; two were run in a live session with no
    ill effect.
"""
import io
import json
import os
import tempfile

import hou
from PySide6 import QtCore, QtGui, QtWidgets

_STORE = os.path.join(tempfile.gettempdir(), "kimodo_wedge_baseline.json")


def _widget_row(w):
    return {
        "cls": type(w).__name__,
        "name": w.objectName(),
        "enabled": w.isEnabled(),
        "visible": w.isVisible(),
        "WA_Disabled": w.testAttribute(QtCore.Qt.WA_Disabled),
        "WA_TransparentForMouseEvents": w.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents),
        "updatesEnabled": w.updatesEnabled(),
        "size": (w.width(), w.height()),
    }


def capture():
    app = QtWidgets.QApplication.instance()
    gapp = QtGui.QGuiApplication.instance()
    mw = gapp.modalWindow()
    snap = {
        "modalWindow": (mw.title() or "untitled") if mw else None,
        "activeModalWidget": type(app.activeModalWidget()).__name__ if app.activeModalWidget() else None,
        "activePopupWidget": type(app.activePopupWidget()).__name__ if app.activePopupWidget() else None,
        "mouseGrabber": type(QtWidgets.QWidget.mouseGrabber()).__name__ if QtWidgets.QWidget.mouseGrabber() else None,
        "keyboardGrabber": type(QtWidgets.QWidget.keyboardGrabber()).__name__ if QtWidgets.QWidget.keyboardGrabber() else None,
        "overrideCursor": str(app.overrideCursor().shape()) if app.overrideCursor() else None,
        "mouseButtons": str(app.mouseButtons()),
        "modifiers": str(app.queryKeyboardModifiers()),
        "n_toplevel": len(app.topLevelWidgets()),
        "n_visible_toplevel": len([w for w in app.topLevelWidgets() if w.isVisible()]),
        "n_windows": len(gapp.topLevelWindows()),
    }
    # the panes themselves, which is where a missed WA_Disabled would show
    snap["gl_panes"] = [_widget_row(w) for w in app.allWidgets()
                        if w.objectName() == "RE_WindowDrawable"]
    snap["disabled_widgets"] = [_widget_row(w) for w in app.allWidgets()
                                if w.isVisible() and not w.isEnabled()][:25]
    snap["disabled_count"] = len([w for w in app.allWidgets()
                                  if w.isVisible() and not w.isEnabled()])
    return snap


def baseline():
    snap = capture()
    with open(_STORE, "w") as fh:
        json.dump(snap, fh, indent=1)
    print("baseline saved to %s" % _STORE)
    print("  %d GL panes, %d disabled visible widgets" %
          (len(snap["gl_panes"]), snap["disabled_count"]))
    return snap


def report():
    now = capture()
    print("=== disabled visible widgets: %d ===" % now["disabled_count"])
    for row in now["disabled_widgets"]:
        print("   %(cls)s/%(name)s  WA_Disabled=%(WA_Disabled)s  size=%(size)s" % row)
    dead = [g for g in now["gl_panes"] if not g["enabled"] or g["WA_Disabled"]]
    if dead:
        print("*** FOUND IT: %d Houdini GL pane(s) are disabled ***" % len(dead))
        for row in dead:
            print("   %s" % row)
    else:
        print("GL panes all enabled and mouse-accepting; the cause is elsewhere.")

    if os.path.exists(_STORE):
        with open(_STORE) as fh:
            was = json.load(fh)
        print("=== changed since baseline ===")
        for k in sorted(was):
            if k in ("gl_panes", "disabled_widgets"):
                continue
            if was[k] != now[k]:
                print("   %-28s %r -> %r" % (k, was[k], now[k]))
    else:
        print("(no baseline saved; call baseline() while Houdini is healthy)")
    return now


# -- live event trace ---------------------------------------------------------
# Every reading above is a snapshot, and every snapshot taken during a wedge has read
# clean. So record the events instead. The one question a snapshot cannot answer:
# while wedged, does Houdini's own pane widget still RECEIVE a QMouseEvent?
#   it does     -> Qt delivery is fine, Houdini's internal dispatch is what broke
#   it does not -> something upstream of the pane is eating the event
# Those two point at different fixes, which is why guessing has not worked.
#
# The log goes to disk as it happens, flushed per line. A wedged session ends in a
# restart, and the first attempt at this lost the whole trace with it.

TRACE_LOG = os.path.join(tempfile.gettempdir(), "kimodo_wedge_trace.log")

_TRACER = None


def _ancestry(w):
    """(top-level description, is it inside the Kimodo panel)."""
    top, inside = w, False
    while True:
        if type(top).__name__ == "TimelineWidget":
            inside = True
        parent = top.parentWidget()
        if parent is None:
            break
        top = parent
    return "%s/%s" % (type(top).__name__, top.objectName()), inside


class _Tracer(QtCore.QObject):
    KINDS = {
        QtCore.QEvent.MouseButtonPress: "press",
        QtCore.QEvent.MouseButtonRelease: "release",
        QtCore.QEvent.MouseButtonDblClick: "dblclk",
        QtCore.QEvent.ContextMenu: "ctxmenu",
        QtCore.QEvent.NonClientAreaMouseButtonPress: "nc-press",
        QtCore.QEvent.NonClientAreaMouseButtonRelease: "nc-release",
        QtCore.QEvent.Show: "show",
        QtCore.QEvent.Hide: "hide",
    }
    # show/hide is noise for everything except the windows this bug is about
    WINDOWED = ("QMenu", "PromptDialog", "QDialog")

    def __init__(self, fh):
        QtCore.QObject.__init__(self)
        self.fh = fh
        self.t0 = QtCore.QDateTime.currentMSecsSinceEpoch()

    def write(self, line):
        self.fh.write("%7d ms  %s\n"
                      % (QtCore.QDateTime.currentMSecsSinceEpoch() - self.t0, line))
        self.fh.flush()

    def eventFilter(self, obj, ev):
        kind = self.KINDS.get(ev.type())
        if kind and obj.isWidgetType():
            cls = type(obj).__name__
            if kind in ("show", "hide") and cls not in self.WINDOWED:
                return False
            top, inside = _ancestry(obj)
            pop = QtWidgets.QApplication.activePopupWidget()
            # the object id matters: it separates "one event delivered twice" from
            # "two different widgets", and those are different bugs
            self.write("%-10s %-22s #%012x %-26s panel=%-5s popup=%-10s btn=%-24s at=%s"
                       % (kind, cls[:22], id(obj), top[:26], inside,
                          type(pop).__name__ if pop else "-",
                          str(QtWidgets.QApplication.mouseButtons()),
                          (QtGui.QCursor.pos().x(), QtGui.QCursor.pos().y())))
        return False


def trace(on=True):
    """Start (or stop) recording mouse events application-wide, to TRACE_LOG."""
    global _TRACER
    app = QtWidgets.QApplication.instance()
    if _TRACER is not None:
        app.removeEventFilter(_TRACER)
        _TRACER.fh.close()
        _TRACER = None
    if not on:
        print("tracing stopped")
        return
    _TRACER = _Tracer(io.open(TRACE_LOG, "w", encoding="utf-8"))
    app.installEventFilter(_TRACER)
    print("tracing to %s" % TRACE_LOG)
    print("call mark('...') to label each step")


def mark(text):
    """Label the point the trace has reached, so the log reads as steps, not soup."""
    if _TRACER is None:
        print("not tracing; call trace() first")
        return
    _TRACER.write("---------------- %s ----------------" % text)
    print("mark: %s" % text)
