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


def splice(old, new, cut: int, n: int, resume_at=None) -> dict:
    """Keep `old` up to the transition, then the returned clip whole.

    `new`'s first `n` samples are the blended seam the model produced for those frames,
    so they replace old[cut - n:cut] rather than being dropped. `resume_at` continues
    with the rest of the original clip afterwards, for a single-sequence replacement.
    """
    out = {}
    for k in CLIP_KEYS:
        if k in old and k in new:
            parts = [old[k][:cut - n], new[k]]
            if resume_at is not None:
                parts.append(old[k][resume_at:])
            out[k] = np.concatenate(parts, axis=0)
    for k in old:                            # anything not per-sample rides along
        if k not in out:
            out[k] = old[k]
    return out


def seam_jump(merged, at: int) -> float:
    """Max joint movement across the join at sample `at`."""
    pj = merged["posed_joints"]
    return float(np.linalg.norm(pj[at] - pj[at - 1], axis=-1).max())


def mean_step(clip) -> float:
    """The clip's own mean per-sample joint movement, the scale a seam is judged against.
    Below 1.0x means the join moves less than the motion around it, which is the point at
    which it stops reading as a cut.
    """
    d = np.linalg.norm(np.diff(clip["posed_joints"], axis=0), axis=-1).max(axis=1)
    return float(d.mean())


def tail_pin(npz, cut_end: int, n: int, at_index: int) -> list:
    """Constraints holding the new sequence's last `n` frames to the `n` samples before
    `cut_end`, so whatever follows in the existing clip still joins on.

    Indices are sequence-relative: the model crops user constraints with current_frame = 0
    for the first requested sequence and prepends the transition afterwards. Full body plus
    end effectors, the same pairing the model uses for its own transitions.
    """
    head = slice(cut_end - n, cut_end)
    pos = npz["posed_joints"][head].tolist()
    rot = npz["global_rot_mats"][head].tolist()
    root = npz["smooth_root_pos"][head][:, [0, 2]].tolist()
    fi = list(range(at_index, at_index + n))
    common = {"frame_indices": fi, "global_joints_positions": pos,
              "global_joints_rots": rot, "smooth_root_2d": root}
    return [dict(common, type="fullbody-global"),
            dict(common, type="ee-global",
                 joint_names=["LeftHand", "RightHand", "LeftFoot", "RightFoot"])]


def samples_for(frames: int, scene_fps: float, source_fps: float) -> int:
    """Samples the server will generate for a sequence: it uses int(duration * fps)."""
    return max(1, int(frames / scene_fps * source_fps))


def load_npz_bytes(blob: bytes) -> dict:
    """Read every array out of the archive once; an NpzFile re-decompresses per lookup."""
    return dict(np.load(io.BytesIO(blob)))


# -- Houdini side -------------------------------------------------------------
# Below the pure functions so the module still imports without hou.

def regenerate(node, seg_index: int, to_end: bool = False):
    """Re-roll sequence `seg_index` (0-based), in place.

    By default only that sequence: its head is joined with continue_from and its tail is
    held to the frames the next sequence was generated against, so the clip keeps its
    length and everything either side is untouched. Measured on a 601-sample clip, that
    leaves the two joins at 0.80x and 0.39x of the clip's own per-sample motion, against
    31.83x for the tail with no pin.

    `to_end` re-rolls this sequence and every one after it instead, which is what you
    want when the change should carry through the rest of the clip.

    Returns as soon as the job is queued; a JobWatcher on Houdini's event loop finishes
    the merge when the clip arrives, so Houdini stays interactive throughout. Progress
    and the final seam measurement land on the node's Status parm, which the Timeline
    panel displays.
    """
    import os

    import hou
    import requests

    from .model import Timeline

    tl = Timeline.from_json(node.parm("timeline_json").eval())
    if not tl.segments:
        raise ValueError("This node has no timeline to regenerate from.")
    if not 0 <= seg_index < len(tl.segments):
        raise ValueError("Sequence %d is outside the timeline." % (seg_index + 1))
    if seg_index == 0:
        raise ValueError("Sequence 1 has no earlier motion to continue from; use Generate.")
    src = node.parm("npz_path").eval()
    if not src or not os.path.exists(src):
        raise ValueError("No generated clip on this node yet. Press Generate first.")

    n = max(1, int(tl.transition_frames))
    scene_fps = float(hou.fps())
    source_fps = float(node.parm("source_fps").eval() or 30)
    frames = [s.frames for s in tl.segments]
    cut = cut_sample(frames, seg_index, scene_fps, source_fps)

    old = dict(np.load(src))     # read the archive once; an NpzFile re-decompresses per lookup
    have = old["posed_joints"].shape[0]
    # A bounds check is not enough: edited sequence lengths still produce an in-range cut,
    # just the wrong one. Compare what the timeline describes against what is on disk.
    expect = int(round(sum(frames) * source_fps / scene_fps))
    if abs(expect - have) > 2 * n:
        raise ValueError(
            "The clip on disk is %d samples but this timeline describes %d. The sequence "
            "lengths changed since it was generated, so the cut would land in the wrong "
            "place. Press Generate first." % (have, expect))
    if cut - n < 1 or cut > have:
        raise ValueError("Sequence %d falls outside the clip on disk; press Generate."
                         % (seg_index + 1))

    # the last sequence has nothing after it, so a single re-roll and a run to the end
    # are the same thing
    single = not to_end and seg_index + 1 < len(tl.segments)
    send = tl.segments[seg_index:seg_index + 1] if single else tl.segments[seg_index:]
    body = {
        "segments": Timeline(send).request_segments(scene_fps),
        "transition_frames": n,
        "model": node.parm("model").evalAsString(),
        "force": bool(node.parm("force").eval()),
        "continue_from": continue_payload(old, cut, n),
    }
    resume_at = None
    if single:
        resume_at = cut_sample(frames, seg_index + 1, scene_fps, source_fps)
        nf = samples_for(frames[seg_index], scene_fps, source_fps)
        body["constraints"] = tail_pin(old, resume_at, n, nf - n)

    url = node.parm("server_url").eval().rstrip("/")
    resp = requests.post(url + "/generate", json=body, timeout=60)
    if resp.status_code == 422:
        raise ValueError("Server rejected `continue_from`; it predates partial "
                         "regeneration. Update kimodo_server.py and kimodo_model.py, "
                         "then restart the api container.")
    resp.raise_for_status()
    job = resp.json()["job_id"]

    what = "sequence %d" % (seg_index + 1) if single else "from sequence %d" % (seg_index + 1)

    def _merge(data, suffix):
        """Runs on the main thread when the job finishes, from the event-loop callback."""
        blob = requests.get("%s/jobs/%s/download" % (url, job), timeout=180).content
        new = load_npz_bytes(blob)
        merged = splice(old, new, cut, n, resume_at=resume_at)
        head_at = cut - n
        normal = mean_step(old)
        jump = seam_jump(merged, head_at)
        joins = "%.2f cm" % (jump * 100)
        worst = jump
        if resume_at is not None:                  # a single sequence has two joins
            tail_jump = seam_jump(merged, head_at + new["posed_joints"].shape[0])
            joins = "%.2f / %.2f cm" % (jump * 100, tail_jump * 100)
            worst = max(worst, tail_jump)

        out = os.path.join(os.path.dirname(src), "%s_joined.npz" % job)
        np.savez(out, **merged)
        total = merged["posed_joints"].shape[0]
        secs = total / source_fps
        node.parm("npz_path").set(out.replace(chr(92), "/"))
        node.parm("clip_info").set(
            "%.2f s = %d frames @ %g fps (%d samples @ %g fps)"
            % (secs, round(secs * scene_fps), scene_fps, total, source_fps))
        node.parm("progress").set(1.0)
        msg = ("Regenerated %s; seam %s vs %.2f cm/sample (%.2fx)"
               % (what, joins, normal * 100, worst / normal))
        node.parm("status").set(msg)
        node.parm("job_id").set("")
        node.cook(force=True)
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Kimodo: " + msg, severity=hou.severityType.ImportantMessage)

    from .poller import JobWatcher
    node.parm("job_id").set(job)
    node.parm("progress").set(0.0)
    node.parm("status").set("Queued (%s...)" % job[:8])
    JobWatcher(node, url, job, "Regenerating " + what, _merge).start()
    return "Regenerating %s; watch the node's Status." % what
