"""A progress dialog that shows up for short jobs, unlike the two defaults.

hou.InterruptableOperation hides its dialog until HOUDINI_INTERRUPT_THRESH seconds have
passed, and QProgressDialog does the same on its own terms: minimumDuration defaults to
4000 ms and Qt's docs say that below it "the dialog will not appear at all". Measured on
a 2 s generation, reporting from 0.005 s at 13 Hz, neither ever appeared. Setting
minimumDuration to 0 and calling forceShow puts it on screen immediately, per dialog,
with no environment variable and no effect on the rest of Houdini.

Modality earns its keep twice over: Qt documents that a modal QProgressDialog's setValue
calls processEvents, so Houdini keeps redrawing while we poll instead of sitting frozen
behind a blocked main thread, and Cancel becomes a plain wasCanceled() check.

Qt's own warning applies to us, and it is the bug we already hit elsewhere: "don't use a
QProgressDialog inside a paintEvent". Construct this from the event loop, never from
inside a Qt event handler. widget.later() is how the panel does that.
"""
from __future__ import annotations

import hou
from PySide6 import QtCore, QtWidgets

STEPS = 1000          # dialog range; progress arrives as a 0..1 fraction


class JobProgress:
    """Context manager around a QProgressDialog, driven by a 0..1 fraction."""

    def __init__(self, title: str, label: str = ""):
        self._title, self._label, self.dlg = title, label, None

    def __enter__(self) -> "JobProgress":
        self.dlg = QtWidgets.QProgressDialog(self._label, "Cancel", 0, STEPS,
                                             hou.qt.mainWindow())
        self.dlg.setWindowTitle(self._title)
        self.dlg.setWindowModality(QtCore.Qt.WindowModal)   # so setValue pumps for us
        self.dlg.setStyleSheet(hou.ui.qtStyleSheet())
        self.dlg.setMinimumDuration(0)     # the 4000 ms default is why nothing showed
        self.dlg.setAutoClose(False)       # closing is __exit__'s job, not reset()'s
        self.dlg.setAutoReset(False)
        self.dlg.setValue(0)
        self.dlg.forceShow()
        return self

    def update(self, fraction: float, label: str | None = None) -> None:
        """Report 0..1, optionally relabelling. Pumps the event loop either way.

        setValue only pumps when the value actually changes, and a polling loop spends
        most of its time reporting the same number, so pump explicitly as well.
        """
        if label is not None:
            self.dlg.setLabelText(label)
        self.dlg.setValue(max(0, min(STEPS, int(fraction * STEPS))))
        QtWidgets.QApplication.processEvents()

    @property
    def cancelled(self) -> bool:
        return bool(self.dlg.wasCanceled())

    def __exit__(self, *exc) -> bool:
        self.dlg.close()
        self.dlg.deleteLater()            # parented to the main window, so it would linger
        self.dlg = None
        return False
