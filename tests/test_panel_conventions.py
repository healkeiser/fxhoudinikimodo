"""Guards for the panel rules that were learned the hard way, in a real Houdini
session.

Each rule here corresponds to a bug that actually shipped and locked Houdini's
panes out of the mouse, or broke the panel outright. They are cheap source
checks, no Houdini and no Qt required, so they run anywhere:

    python tests/test_panel_conventions.py

They do not replace the live-session test (tests/test_houdini_live.py), which
drives the real widgets. They catch the same mistakes far earlier and for free.
"""

import ast
import io
import sys
import tokenize
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "houdini" / "python" / "kimodo_timeline"
HDA_SRC = HERE.parent / "scripts" / "create_hda.py"


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _code(src):
    """`src` with comments and docstrings blanked out, offsets and line numbers
    intact.

    Most rules below are substring checks, and the panel's comments have to stay
    free to name the very thing they are warning about. Three of these guards
    failed on their own explanation before this existed, which is a bad guard,
    not a bad comment. Only comments and docstrings are blanked, never a string
    used as a value, so the result still parses and _func_source still works on
    it.
    """
    out = list(src)
    starts, total = [], 0
    for line in src.splitlines(keepends=True):
        starts.append(total)
        total += len(line)

    def blank(begin, end):
        for i in range(
            starts[begin[0] - 1] + begin[1], starts[end[0] - 1] + end[1]
        ):
            if out[i] != chr(10):
                out[i] = " "

    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            blank(tok.start, tok.end)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            begin = (first.lineno, first.col_offset)
            blank(begin, (first.end_lineno, first.end_col_offset))
            if len(body) == 1:
                # Sole statement: blanking it all out leaves an empty body,
                # and the result no longer parses. A docstring is never
                # shorter than "pass".
                at = starts[begin[0] - 1] + begin[1]
                out[at : at + 4] = "pass"
    return "".join(out)


def _modules():
    """Every panel module, comments and docstrings blanked. Use _read for raw
    source.
    """
    for path in sorted(PKG.glob("*.py")):
        yield path.name, _code(_read(path))


def _func_source(src, name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("function %s not found" % name)


def _called_names(src):
    """Every name that is actually called. Beats a substring scan: the panel's
    comments have to be free to name the thing they are warning about."""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(
                f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            )
    return out


def test_only_the_context_menu_runs_a_nested_loop():
    """A nested Qt event loop inside Houdini's UI pump is the prime suspect for
    the input wedge: after one, every native mouse message is delivered to
    Houdini's panes twice (press, press, release, release), so their
    press/release pairing never rebalances. Traced event by event in a live
    session, twice.

    The one exception is the context menu, because fxhoucachemanager runs exec
    on a hou.qt.Menu and has never wedged Houdini. That is the shape we copied,
    so it is the shape allowed here: exec only via run_exec, only in
    contextMenuEvent. Nothing else, and no processEvents pump anywhere, which is
    only exec spelled differently."""
    for name, src in _modules():
        assert "processEvents" not in _called_names(src), (
            "%s pumps processEvents; that is a nested event loop wearing a hat"
            % name
        )
        assert not (_called_names(src) & {"exec", "exec_"}), (
            "%s calls exec directly; go through qt.run_exec" % name
        )
    src = _code(_read(PKG / "widget.py"))
    menu_fn = _func_source(src, "contextMenuEvent")
    assert src.count("run_exec(") == 1, (
        "run_exec has escaped contextMenuEvent; only the menu may run a nested loop"
    )
    assert "run_exec(menu" in menu_fn, (
        "the context menu is the one sanctioned exec"
    )


def test_the_dialog_is_shown_not_run():
    """_ask hands its result to a callback rather than returning it, because
    returning one would mean waiting for it, and waiting means a nested loop."""
    src = _code(_read(PKG / "widget.py"))
    ask = _func_source(src, "_ask")
    assert ".show()" in ask, "_ask must show() the dialog"
    assert "finished.connect" in ask, (
        "_ask must continue from finished, not from a wait"
    )
    assert "WA_DeleteOnClose" in ask, (
        "a main-window-parented dialog needs a lifetime"
    )


def test_context_menu_handlers_run_after_the_menu_closes():
    """fxhoucachemanager reads the chosen action back from exec and runs it
    afterwards, so nothing of its own runs inside the menu's event loop.
    Connecting to `triggered` would put our Houdini writes back inside that
    loop."""
    src = _code(_read(PKG / "widget.py"))
    menu_fn = _func_source(src, "contextMenuEvent")
    assert "triggered" not in menu_fn, (
        "do not connect to triggered; dispatch after exec"
    )
    assert "addAction(" in menu_fn and "lambda" in menu_fn, (
        "actions map to callables"
    )


def test_no_interruptable_operation_in_the_panel():
    """Blocking the main thread to hold a progress dialog is what wedged
    Houdini. Jobs are polled from the event loop instead (poller.JobWatcher)."""
    for name, src in _modules():
        assert "InterruptableOperation" not in src, (
            "%s must not block on an operation" % name
        )
    assert "InterruptableOperation" not in _code(_read(HDA_SRC)), (
        "the HDA callback must not either"
    )


def test_nothing_sleeps():
    """A sleep on the main thread starves the event loop; the event-loop
    callback replaced every poll loop that used to."""
    for name, src in _modules():
        assert "time.sleep" not in src, "%s must not sleep" % name
    assert "time.sleep" not in _code(_read(HDA_SRC)), (
        "the HDA callback must not sleep"
    )


def test_only_the_shim_names_a_qt_binding():
    """Houdini 19.5 ships PySide2, 20+ ships PySide6. Everything imports from
    .qt so the binding is chosen in exactly one place."""
    for name, src in _modules():
        if name == "qt.py":
            continue
        for binding in ("PySide2", "PySide6", "qtpy"):
            assert binding not in src, (
                "%s names %s; import from .qt instead" % (name, binding)
            )


def test_no_qt6_only_calls_outside_the_shim():
    """position() and exec() are Qt6 spellings; Qt5 uses localPos()/posF() and
    exec_(). qt.event_pos papers over both."""
    for name, src in _modules():
        if name == "qt.py":
            continue
        assert ".position()" not in src, (
            "%s uses Qt6-only position(); use event_pos()" % name
        )


def test_context_menu_is_houdinis_own():
    """hou.qt.Menu is built by Houdini's C++ factory, so Houdini knows the popup
    exists and it comes pre-styled. A plain QtWidgets.QMenu is invisible to
    Houdini. SideFX use hou.qt.Menu in their own panels (pdgservicepanel.py,
    paintinstances/panel.py).

    One reference on self, because popup() returns before the menu is used and
    an unparented menu would otherwise die with this frame. The next right-click
    replaces it: one menu at a time, so the four-alive-in-one-session leak
    cannot come back."""
    src = _code(_read(PKG / "widget.py"))
    called = _called_names(
        src
    )  # calls, not text: the comments explain the trap
    assert "Menu" in called, "build the context menu with hou.qt.Menu()"
    assert "QMenu" not in called, (
        "a plain QtWidgets.QMenu is invisible to Houdini"
    )
    assert "self._menu = menu" in _func_source(src, "contextMenuEvent"), (
        "the menu needs one reference to survive popup()"
    )


def test_the_canvas_never_leaks_a_press_into_houdinis_pane():
    """The input wedge, finally, and it was never about menus.

    Canvas.mouseReleaseEvent consumes every release. mousePressEvent used to
    hand any non-left button to super(), whose default implementation IGNORES
    the event, so it propagated up the parent chain. The panel is a Python
    Panel, so that chain runs into QOpenGLWidget/RE_WindowDrawable: Houdini's
    own pane. Traced in a live session, one right-button press reached five
    receivers while its release reached one. Houdini's pane was left holding a
    button that never came up, and from then on every pane except this one
    ignored the mouse.

    Press and release must consume the same buttons. An interactive canvas has
    no business letting an ancestor see its mouse events at all."""
    src = _code(_read(PKG / "widget.py"))
    press = _func_source(src, "mousePressEvent")
    assert "super()" not in press, (
        "a press given to super() is ignored, and then propagates"
    )
    assert "ignore()" not in press, (
        "an ignored press propagates into Houdini's pane"
    )
    assert "accept()" in press, (
        "consume the press the same way the release is consumed"
    )
    release = _func_source(src, "mouseReleaseEvent")
    assert "super()" not in release and "ignore()" not in release, (
        "if the release starts propagating, the press must too"
    )


def test_deferral_waits_for_the_mouse_release():
    """Houdini tracks mouse buttons globally across every Qt widget and clears
    that state on the next release (hou.qt.skipClosingMenusForCurrentButtonPress
    documents both), so later() runs fn only once nothing is held. Belt and
    braces, not the cure for the input wedge: that was the nested event loop,
    see test_nothing_opens_a_nested_event_loop."""
    src = _code(_read(PKG / "widget.py"))
    fn = _func_source(src, "later")
    assert "mouseButtons()" in fn, (
        "later() must not run while a mouse button is held"
    )
    assert "NoButton" in fn, "later() must compare against Qt.NoButton"


def test_status_text_is_always_elided():
    """Status carries server errors verbatim and they can be a paragraph long. A
    label left to size itself widens the footer and drags the whole panel out
    with it, so every writer must go through _set_status, which elides to
    STATUS_W and puts the full text in the tooltip."""
    src = _code(_read(PKG / "widget.py"))
    setter = _func_source(src, "_set_status")
    assert "elidedText" in setter, "_set_status must elide"
    assert "setToolTip" in setter, (
        "_set_status must keep the full text in the tooltip"
    )
    assert "STATUS_W" in src, "the status label needs a width cap"
    body = src.replace(setter, "")
    assert "status_label.setText(" not in body, (
        "write status through _set_status, not status_label.setText"
    )


def test_source_is_ascii():
    """Escapes, not literal glyphs, so the files survive any encoding they pass
    through. Raw source on purpose: a stray glyph in a comment counts."""
    for path in sorted(PKG.glob("*.py")):
        src = _read(path)
        bad = [
            (i + 1, line)
            for i, line in enumerate(src.splitlines())
            if any(ord(c) > 126 for c in line)
        ]
        assert not bad, "%s has literal non-ASCII on line(s) %s" % (
            path.name,
            [b[0] for b in bad],
        )


def test_poller_never_touches_hou_ui_at_import():
    """hou.ui does not exist in hython. The module must import there and refuse
    politely only when something actually tries to start a watch."""
    src = _read(PKG / "poller.py")
    tree = ast.parse(src)

    def is_docstring(n):
        return (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )

    toplevel = [
        n
        for n in tree.body
        if not isinstance(n, (ast.FunctionDef, ast.ClassDef))
        and not is_docstring(n)
    ]
    joined = "\n".join(ast.get_source_segment(src, n) or "" for n in toplevel)
    assert "hou.ui" not in joined, (
        "poller.py must not touch hou.ui at import time"
    )
    assert "isUIAvailable" in src, (
        "JobWatcher.start must refuse a non-UI session"
    )


def test_a_precondition_is_a_warning_not_an_error():
    """The guards in regen.regenerate say which button to press first ("press
    Generate"); nothing is broken, the user asked out of order. Both Regenerate
    paths have to colour that one as a warning, and leave last_error alone,
    because the cook script turns last_error into a hou.NodeError and reddens
    the node.
    """
    hda = _read(HDA_SRC)  # run_regenerate lives in an embedded script string
    start = hda.index("\ndef run_regenerate(")
    for src, fn in (
        (_code(_read(PKG / "widget.py")), "_regen"),
        (hda[start : hda.index("\ndef ", start + 1)], "run_regenerate"),
    ):
        caught = [
            ast.unparse(h)
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.FunctionDef) and node.name == fn
            for h in ast.walk(node)
            if isinstance(h, ast.ExceptHandler)
            and h.type is not None
            and "Precondition" in ast.unparse(h.type)
        ]
        assert len(caught) == 1, (
            "%s must catch regen.Precondition apart from the real failures" % fn
        )
        assert "severityType.Warning" in caught[0], (
            "%s shows a precondition in the status bar's error red" % fn
        )
        assert "last_error" not in caught[0], (
            "%s reddens the node over a precondition" % fn
        )


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
