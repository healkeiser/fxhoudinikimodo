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
