"""Guards for the panel rules that were learned the hard way, in a real Houdini session.

Each rule here corresponds to a bug that actually shipped and locked Houdini's panes out
of the mouse, or broke the panel outright. They are cheap source checks, no Houdini and
no Qt required, so they run anywhere:

    python tests/test_panel_conventions.py

They do not replace the live-session test (tests/test_houdini_live.py), which drives the
real widgets. They catch the same mistakes far earlier and for free.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "..", "houdini", "python", "kimodo_timeline")
HDA_SRC = os.path.join(HERE, "..", "scripts", "create_hda.py")


def _read(path):
    return io.open(path, encoding="utf-8").read()


def _modules():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            yield name, _read(os.path.join(PKG, name))


def _func_source(src, name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("function %s not found" % name)


def test_no_modal_exec_on_dialogs():
    """QDialog.exec enters Qt's modal loop, which leaves a Houdini operation scope open:
    afterwards every Houdini pane ignores the mouse until a restart. _ask must show() the
    dialog and pump its own loop, the way SideFX's own panels do."""
    src = _read(os.path.join(PKG, "widget.py"))
    ask = _func_source(src, "_ask")
    assert ".show()" in ask, "_ask must show() the dialog, not exec() it"
    for bad in (".exec(", ".exec_(", "run_exec("):
        assert bad not in ask, "_ask must not use %s: it leaks an operation scope" % bad


def test_no_interruptable_operation_in_the_panel():
    """Blocking the main thread to hold a progress dialog is what wedged Houdini. Jobs are
    polled from the event loop instead (poller.JobWatcher)."""
    for name, src in _modules():
        assert "InterruptableOperation" not in src, "%s must not block on an operation" % name
    assert "InterruptableOperation" not in _read(HDA_SRC), "the HDA callback must not either"


def test_no_stray_process_events():
    """processEvents lets arbitrary queued work run re-entrantly. The only sanctioned use
    is the dialog wait in _ask, which is SideFX's own pattern."""
    for name, src in _modules():
        if "processEvents" not in src:
            continue
        ask = _func_source(src, "_ask") if name == "widget.py" else ""
        outside = src.count("processEvents") - ask.count("processEvents")
        assert outside == 0, "%s calls processEvents outside the dialog wait" % name


def test_nothing_sleeps():
    """A sleep on the main thread starves the event loop; the event-loop callback replaced
    every poll loop that used to."""
    for name, src in _modules():
        assert "time.sleep" not in src, "%s must not sleep" % name
    assert "time.sleep" not in _read(HDA_SRC), "the HDA callback must not sleep"


def test_only_the_shim_names_a_qt_binding():
    """Houdini 19.5 ships PySide2, 20+ ships PySide6. Everything imports from .qt so the
    binding is chosen in exactly one place."""
    for name, src in _modules():
        if name == "qt.py":
            continue
        for binding in ("PySide2", "PySide6", "qtpy"):
            assert binding not in src, "%s names %s; import from .qt instead" % (name, binding)


def test_no_qt6_only_calls_outside_the_shim():
    """position() and exec() are Qt6 spellings; Qt5 uses localPos()/posF() and exec_().
    qt.event_pos and qt.run_exec paper over both."""
    for name, src in _modules():
        if name == "qt.py":
            continue
        assert ".position()" not in src, "%s uses Qt6-only position(); use event_pos()" % name


def test_context_menu_is_unparented():
    """A QMenu parented to the Canvas outlives the right-click that made it; four were
    alive in one session. SideFX's own QuickStart panel builds it unparented, so Python
    owns it and it dies with the local. Nothing to delete, nothing to leak."""
    src = _read(os.path.join(PKG, "widget.py"))
    menu_fn = _func_source(src, "contextMenuEvent")
    assert "QtWidgets.QMenu()" in menu_fn, "build the context menu unparented"
    assert "QtWidgets.QMenu(self)" not in menu_fn, "parenting the menu to the Canvas leaks it"


def test_source_is_ascii():
    """Escapes, not literal glyphs, so the files survive any encoding they pass through."""
    for name, src in _modules():
        bad = [(i + 1, line) for i, line in enumerate(src.splitlines())
               if any(ord(c) > 126 for c in line)]
        assert not bad, "%s has literal non-ASCII on line(s) %s" % (name, [b[0] for b in bad])


def test_poller_never_touches_hou_ui_at_import():
    """hou.ui does not exist in hython. The module must import there and refuse politely
    only when something actually tries to start a watch."""
    src = _read(os.path.join(PKG, "poller.py"))
    tree = ast.parse(src)
    def is_docstring(n):
        return (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str))
    toplevel = [n for n in tree.body
                if not isinstance(n, (ast.FunctionDef, ast.ClassDef))
                and not is_docstring(n)]
    joined = "\n".join(ast.get_source_segment(src, n) or "" for n in toplevel)
    assert "hou.ui" not in joined, "poller.py must not touch hou.ui at import time"
    assert "isUIAvailable" in src, "JobWatcher.start must refuse a non-UI session"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                failures += 1
                print("FAIL", name, "--", e)
    sys.exit(1 if failures else 0)
