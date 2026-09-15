"""Qt binding for the panel, through qtpy.

Houdini 19.5 and older ship PySide2, 20 and newer ship PySide6. qtpy hides that, so the
panel does not name a binding anywhere. It is not part of Houdini, so the setup guide
installs it into vendor/, which the package file appends to PYTHONPATH.

The fallback below is a safety net for someone who installed the HDA without that step,
not a supported path: it picks whatever binding Houdini happens to ship. Importing Qt in
one place also gives any future compatibility shim a single seam to live in.

Imports are only half of supporting both bindings. The two helpers below cover the calls
that are actually spelled differently, so nothing else in the panel names a Qt version.
"""
try:
    from qtpy import QtCore, QtGui, QtWidgets          # noqa: F401
except ImportError:                                    # vendor/ step skipped
    try:
        from PySide6 import QtCore, QtGui, QtWidgets   # noqa: F401
    except ImportError:                                # Houdini 19.5 and older
        from PySide2 import QtCore, QtGui, QtWidgets   # noqa: F401

__all__ = ["QtCore", "QtGui", "QtWidgets", "event_pos", "run_exec"]


def event_pos(ev):
    """Cursor position in widget coordinates, as a QPointF, on either binding.

    Qt6 renamed QMouseEvent.localPos() and QWheelEvent.posF() to position(); Qt5 has no
    position() at all. Asking the event what it supports beats branching on a version.
    """
    if hasattr(ev, "position"):          # Qt6
        return ev.position()
    if hasattr(ev, "localPos"):          # Qt5 mouse events
        return ev.localPos()
    return ev.posF()                     # Qt5 wheel events


def run_exec(widget, *args):
    """Run a modal QDialog or QMenu. PySide2 spells it exec_(), Qt6 exec()."""
    return (getattr(widget, "exec", None) or widget.exec_)(*args)
