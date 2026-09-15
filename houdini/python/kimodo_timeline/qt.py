"""Qt binding for the panel, through qtpy when the environment provides one.

Houdini 19.5 and older ship PySide2, 20 and newer ship PySide6. qtpy papers over the
import differences and is what our packages provide, but it is not part of Houdini, so
fall back to the binding directly rather than failing on a stock install. Importing Qt
in one place also gives any future compatibility shim a single seam to live in.

Imports are only the easy half of supporting both. This module does not make the panel
PySide2-clean on its own: widget.py uses QMouseEvent.position(), which is Qt6 only
(Qt5 spells it pos()/localPos()), and QDialog.exec()/QMenu.exec(), which PySide2 spells
exec_(). Those need fixing before claiming Houdini 19.5 support.
"""
try:
    from qtpy import QtCore, QtGui, QtWidgets          # noqa: F401
except ImportError:                                    # stock Houdini, no qtpy package
    try:
        from PySide6 import QtCore, QtGui, QtWidgets   # noqa: F401
    except ImportError:                                # Houdini 19.5 and older
        from PySide2 import QtCore, QtGui, QtWidgets   # noqa: F401

__all__ = ["QtCore", "QtGui", "QtWidgets"]
