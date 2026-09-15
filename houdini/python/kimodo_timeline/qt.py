"""Qt binding for the panel, through qtpy.

Houdini 19.5 and older ship PySide2, 20 and newer ship PySide6. qtpy hides that, so the
panel does not name a binding anywhere. It is not part of Houdini, so the setup guide
installs it into vendor/, which the package file appends to PYTHONPATH.

The fallback below is a safety net for someone who installed the HDA without that step,
not a supported path: it picks whatever binding Houdini happens to ship. Importing Qt in
one place also gives any future compatibility shim a single seam to live in.

Imports are only the easy half of supporting both bindings. This module does not make the
panel PySide2-clean on its own: widget.py uses QMouseEvent.position(), which is Qt6 only
(Qt5 spells it pos()/localPos()), and QDialog.exec()/QMenu.exec(), which PySide2 spells
exec_(). Those need fixing before claiming Houdini 19.5 support.
"""
try:
    from qtpy import QtCore, QtGui, QtWidgets          # noqa: F401
except ImportError:                                    # vendor/ step skipped
    try:
        from PySide6 import QtCore, QtGui, QtWidgets   # noqa: F401
    except ImportError:                                # Houdini 19.5 and older
        from PySide2 import QtCore, QtGui, QtWidgets   # noqa: F401

__all__ = ["QtCore", "QtGui", "QtWidgets"]
