"""Everything that touches hou lives here; model.py and the tests never import it."""
from __future__ import annotations

import hou

from .model import Timeline

TYPE_PREFIX = "vb::kimodo_motion"


def find_node():
    """First selected Kimodo Motion node, or None."""
    for n in hou.selectedNodes():
        if n.type().name().startswith(TYPE_PREFIX):
            return n
    return None


def all_nodes():
    """Every Kimodo Motion node in the scene, sorted by path."""
    out = []
    for name, nt in hou.sopNodeTypeCategory().nodeTypes().items():
        if name.startswith(TYPE_PREFIX):
            out.extend(nt.instances())
    return sorted(out, key=lambda n: n.path())


def node_at(path):
    """The Kimodo Motion node at `path`, or None."""
    n = hou.node(path) if path else None
    return n if n is not None and n.type().name().startswith(TYPE_PREFIX) else None


def has_timeline(node) -> bool:
    return bool(node.parm("timeline_json").eval().strip())


def load(node) -> Timeline:
    """The node's timeline, seeded from Prompt/Duration/Pose Keyframes if it has none yet."""
    raw = node.parm("timeline_json").eval()
    if raw.strip():
        return Timeline.from_json(raw)
    return Timeline.from_legacy(
        node.parm("prompt").eval(),
        node.parm("duration_frames").eval(),
        node.parm("pose_keyframes").eval(),
    )


def save(node, tl: Timeline, label: str = "Kimodo timeline edit") -> None:
    """Write the timeline back as one undoable step. Duration mirrors the total so the
    node reads right even with the panel closed; Pose Keyframes mirrors the Full Body
    track so legacy readers still see something sensible."""
    with hou.undos.group(label):
        node.parm("timeline_json").set(tl.to_json())
        node.parm("has_timeline").set(1)
        if tl.segments:
            node.parm("duration_frames").set(tl.total_frames)
        node.parm("pose_keyframes").set(" ".join(str(k) for k in tl.tracks.get("fullbody", [])))


def detach(node) -> None:
    """Drop the timeline; the node goes back to Prompt + Duration. Keeps the first
    segment's text as the prompt so nothing is lost."""
    tl = load(node)
    with hou.undos.group("Kimodo timeline detach"):
        if tl.segments:
            node.parm("prompt").set(tl.segments[0].prompt)
        node.parm("timeline_json").set("")
        node.parm("has_timeline").set(0)


def start_frame(node) -> int:
    return int(node.parm("start_frame").eval())


def fps() -> float:
    return float(hou.fps())


def hip_frame_range() -> tuple[int, int]:
    """The HIP file's playbar range (global start/end)."""
    a, b = hou.playbar.frameRange()
    return int(round(a)), int(round(b))


def current_frame() -> int:
    return int(round(hou.frame()))


def set_frame(frame: int) -> None:
    """Set the frame and let Houdini catch up. Without the update the viewport only
    redraws once the mouse is released, so dragging the ruler does not read as scrubbing."""
    hou.setFrame(int(frame))
    if hou.isUIAvailable():
        hou.ui.triggerUpdate()


def status(node) -> str:
    return node.parm("status").eval()


def progress(node) -> float | None:
    """0..1 while a job is queued/running/downloading, else None."""
    st = node.parm("status").eval()
    if st.startswith(("Queued", "Running", "Downloading")):
        return float(node.parm("progress").eval())
    return None


def generate(node) -> None:
    node.parm("generate").pressButton()


def cancel(node) -> None:
    node.parm("cancel").pressButton()
