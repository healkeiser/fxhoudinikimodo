"""Regenerate the tail of a generated clip instead of the whole thing.

Kimodo builds a multi-prompt clip one segment at a time, each segment joined to the
previous by a transition: the previous tail is prepended as observed motion, moved to
the origin, generated against, then moved back and alpha-blended (see `_multiprompt`
in kimodo/model/kimodo_model.py).

A plain /generate always starts with that history empty, so its first segment takes the
"first motion" path and the transition never runs. Handing the tail over as a world-space
constraint does not substitute for it: the constrained frames come back accurate to a
millimetre, but the motion after them continues on its own trajectory, measured 184 cm
away.

So the server takes a `continue_from` tail and seeds that history instead, which makes
the first requested segment join properly. Measured on a 545-sample clip, the seam moves
2.18 cm against the clip's own 10.48 cm mean per sample, so the join is smoother than
ordinary motion.

The returned clip's head IS the blended seam, so the merge follows the model's own
convention, old[:cut - n] + new, with no frames dropped.

The pure functions take plain data so they can be tested without Houdini.
"""
from __future__ import annotations

import io
import numpy as np

# every per-sample array in a Kimodo NPZ, concatenated on axis 0
CLIP_KEYS = ("local_rot_mats", "global_rot_mats", "posed_joints",
             "root_positions", "smooth_root_pos", "foot_contacts",
             "global_root_heading")


def cut_sample(frames_per_segment, seg_index: int, scene_fps: float, source_fps: float) -> int:
    """Clip sample where `seg_index` begins. Segment lengths are in scene frames."""
    scene = sum(int(f) for f in frames_per_segment[:seg_index])
    return int(round(scene * source_fps / scene_fps))


def continue_payload(npz, cut: int, n: int) -> dict:
    """The `n` samples before `cut`, shaped as the server's `continue_from` block.

    Local rotations rather than global: they are what the motion representation is built
    from, and the server's 77 -> model-skeleton slice is the exact inverse of the
    conversion applied on output, so the round trip is lossless.
    """
    if cut < n:
        raise ValueError("need %d samples before the cut, cut is at %d" % (n, cut))
    head = slice(cut - n, cut)
    return {"local_rot_mats": npz["local_rot_mats"][head].tolist(),
            "root_positions": npz["root_positions"][head].tolist()}


def splice(old, new, cut: int, n: int) -> dict:
    """Keep `old` up to the transition, then take the returned clip whole.

    `new`'s first `n` samples are the blended seam the model produced for those frames,
    so they replace old[cut - n:cut] rather than being dropped.
    """
    out = {}
    for k in CLIP_KEYS:
        if k in old.files and k in new.files:
            out[k] = np.concatenate([old[k][:cut - n], new[k]], axis=0)
    for k in old.files:                      # anything not per-sample rides along
        if k not in out:
            out[k] = old[k]
    return out


def seam_error(old, merged, cut: int, n: int):
    """Max joint movement across the join, and the source clip's own mean per-sample
    movement for scale. Below 1.0x means the join moves less than ordinary motion.
    """
    pj = merged["posed_joints"]
    at = cut - n
    jump = float(np.linalg.norm(pj[at] - pj[at - 1], axis=-1).max())
    d = np.linalg.norm(np.diff(old["posed_joints"], axis=0), axis=-1).max(axis=1)
    return jump, float(d.mean())


def load_npz_bytes(blob: bytes):
    return np.load(io.BytesIO(blob))


# -- Houdini side -------------------------------------------------------------
# Below the pure functions so the module still imports without hou.

def regenerate_from(node, seg_index: int, poll: float = 2.0):
    """Regenerate segment `seg_index` (0-based) and everything after it, in place.

    Blocks behind Houdini's progress dialog: a partial regen is short, and the
    alternative is a second copy of the background poller that lives in the generate
    callback. Returns a one-line summary for the node's Status.
    """
    import json
    import os
    import time

    import hou
    import requests

    raw = node.parm("timeline_json").eval().strip()
    tl = json.loads(raw) if raw else {}
    segs = tl.get("segments") or []
    if not segs:
        raise ValueError("This node has no timeline to regenerate from.")
    if not 0 <= seg_index < len(segs):
        raise ValueError("Segment %d is outside the timeline." % (seg_index + 1))
    if seg_index == 0:
        raise ValueError("Segment 1 has no earlier motion to continue from; use Generate.")
    src = node.parm("npz_path").eval()
    if not src or not os.path.exists(src):
        raise ValueError("No generated clip on this node yet. Press Generate first.")

    n = max(1, int(tl.get("transition_frames", 5)))
    scene_fps = float(hou.fps())
    source_fps = float(node.parm("source_fps").eval() or 30)
    cut = cut_sample([int(s["frames"]) for s in segs], seg_index, scene_fps, source_fps)

    old = np.load(src)
    if cut - n < 1 or cut > old["posed_joints"].shape[0]:
        raise ValueError("The clip on disk does not match this timeline; press Generate.")

    body = {
        "segments": [{"prompt": s["prompt"].strip(), "duration": int(s["frames"]) / scene_fps}
                     for s in segs[seg_index:]],
        "transition_frames": n,
        "model": node.parm("model").evalAsString(),
        "force": bool(node.parm("force").eval()),
        "continue_from": continue_payload(old, cut, n),
    }
    url = node.parm("server_url").eval().rstrip("/")
    resp = requests.post(url + "/generate", json=body, timeout=60)
    if resp.status_code == 422:
        raise ValueError("Server rejected `continue_from`; it predates partial "
                         "regeneration. Update kimodo_server.py and kimodo_model.py, "
                         "then restart the api container.")
    resp.raise_for_status()
    job = resp.json()["job_id"]

    label = "Regenerating from segment %d" % (seg_index + 1)
    with hou.InterruptableOperation("Kimodo", label, open_interrupt_dialog=True) as op:
        last = 0.0
        while True:
            time.sleep(poll)
            op.updateProgress(last)
            d = requests.get("%s/jobs/%s" % (url, job), timeout=15).json()
            if d["status"] == "done":
                break
            if d["status"] in ("failed", "cancelled"):
                raise RuntimeError(d.get("error") or d["status"])
            if d.get("progress") is not None:
                last = float(d["progress"])
                op.updateProgress(last)
                op.updateLongProgress(percentage=last,
                                      long_op_status="Running %d%%" % int(last * 100))
        op.updateLongProgress(percentage=1.0, long_op_status="Downloading")
        blob = requests.get("%s/jobs/%s/download" % (url, job), timeout=180).content
        new = load_npz_bytes(blob)

    merged = splice(old, new, cut, n)
    jump, normal = seam_error(old, merged, cut, n)
    out = os.path.join(os.path.dirname(src), "%s_joined.npz" % job)
    np.savez(out, **merged)

    total = merged["posed_joints"].shape[0]
    secs = total / source_fps
    node.parm("npz_path").set(out.replace("\\", "/"))
    node.parm("clip_info").set(
        "%.2f s = %d frames @ %g fps (%d samples @ %g fps)"
        % (secs, round(secs * scene_fps), scene_fps, total, source_fps))
    node.parm("progress").set(1.0)
    node.cook(force=True)
    return ("Regenerated %d of %d segments; seam %.2f cm vs %.2f cm/sample (%.2fx)"
            % (len(segs) - seg_index, len(segs), jump * 100, normal * 100, jump / normal))
