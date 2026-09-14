"""Kimodo Timeline panel for the vb::kimodo_motion SOP.

model.py   timeline arithmetic (pure python, tested)
bridge.py  reading/writing the node (hou)
widget.py  the PySide6 panel
"""


def create_widget():
    """Entry point used by houdini/python_panels/kimodo_timeline.pypanel."""
    from .widget import TimelineWidget
    return TimelineWidget()
