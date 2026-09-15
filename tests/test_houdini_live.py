"""Live-session test: drive the real panel widgets inside a running Houdini.

Run INSIDE Houdini (Python Shell or the MCP bridge), not hython: it needs hou.ui and a
real Qt application.

    exec(open("tests/test_houdini_live.py").read()); print("\n".join(run()))

SCOPE, honestly stated. These assert the hygiene properties we can measure: that the
segment dialog is never modal, that dialogs do not accumulate, that no Qt grab or modal
widget is left behind. They do NOT prove the "Houdini panes stop answering the mouse"
wedge is gone, because no reliable programmatic detector for it has been found:
hou.updateProgressAndCheckForInterrupt() was tried and never raises in this build, inside
or outside a real operation, so it reports nothing. Only clicking a Houdini pane confirms
that, and a human has to do it.
"""
import gc

import hou
from kimodo_timeline.qt import QtCore, QtWidgets
from kimodo_timeline import model, widget


def _app():
    return QtWidgets.QApplication.instance()


def _flush_deletes():
    """Deliver DeferredDelete. processEvents() does NOT, so a hand-rolled flush that uses
    it alone reports objects as leaked when they are merely pending. Measured: one
    PromptDialog survives processEvents and dies on sendPostedEvents."""
    for _ in range(3):      # deleting a widget posts further deletes for its children
        _app().processEvents()
        _app().sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    gc.collect()


def _count(cls):
    _flush_deletes()
    return len([o for o in gc.get_objects() if type(o) is cls])


def _accept_visible_dialog(cls, delay_ms=60):
    """Accept the next dialog of `cls` that appears, from the event loop."""
    def go():
        for w in _app().topLevelWidgets():
            if isinstance(w, cls) and w.isVisible():
                w.accept()
                return
        QtCore.QTimer.singleShot(delay_ms, go)
    QtCore.QTimer.singleShot(delay_ms, go)


def _canvas():
    c = widget.Canvas()
    c.tl = model.Timeline([model.Segment("walk", 24)])
    return c


def test_segment_dialog_is_never_modal():
    """Modality is the property that pulls Houdini into a blocked state. Whatever the
    exact mechanism, the dialog has no business being modal."""
    dlg = widget.PromptDialog("x", 24, hou.qt.mainWindow())
    dlg.show()
    try:
        assert not dlg.isModal(), "the segment dialog is modal"
        assert _app().activeModalWidget() is None, "a modal widget is active"
    finally:
        dlg.close()
        dlg.deleteLater()


def test_ask_round_trips_without_exec():
    """_ask must return the edited values while never entering Qt's modal loop."""
    c = _canvas()
    try:
        _accept_visible_dialog(widget.PromptDialog)
        accepted, text, frames = c._ask("walk", 24)
        assert accepted, "dialog was not accepted; the harness drove it wrong"
        assert frames == 24, "frames came back as %r" % (frames,)
        assert _app().activeModalWidget() is None, "left a modal widget behind"
    finally:
        c.deleteLater()


def test_dialogs_do_not_accumulate():
    """Parented to the main window, a dialog lives until deleted. Three edits must not
    leave three dialogs alive for the rest of the session."""
    c = _canvas()
    try:
        before = _count(widget.PromptDialog)     # delta, not absolute: other tests ran
        for _ in range(3):
            _accept_visible_dialog(widget.PromptDialog)
            c._ask("walk", 24)
        after = _count(widget.PromptDialog)
        assert after <= before, "segment dialogs accumulating: %d -> %d" % (before, after)
    finally:
        c.deleteLater()


# There is deliberately no runtime menu-leak test. It measured Qt's deletion semantics
# rather than our code: creating three QMenus and flushing DeferredDelete reliably leaves
# one alive for a while, with a control that creates none showing no drift. That made it
# flaky without saying anything about contextMenuEvent, which is covered by the source
# guard that it calls deleteLater at all.


def test_no_input_grab_left_behind():
    """Whatever the wedge is, a Qt grab would be one explanation. Assert we never leave
    one, so that explanation can be ruled out rather than guessed at."""
    c = _canvas()
    try:
        _accept_visible_dialog(widget.PromptDialog)
        c._ask("walk", 24)
        assert QtWidgets.QWidget.mouseGrabber() is None, "mouse grab left behind"
        assert QtWidgets.QWidget.keyboardGrabber() is None, "keyboard grab left behind"
        assert _app().activePopupWidget() is None, "popup left behind"
        assert _app().overrideCursor() is None, "override cursor left behind"
    finally:
        c.deleteLater()


def run():
    results = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                results.append("ok   " + name)
            except AssertionError as e:
                results.append("FAIL " + name + " -- " + str(e))
            except Exception as e:
                results.append("ERR  " + name + " -- %s: %s" % (type(e).__name__, e))
    return results
