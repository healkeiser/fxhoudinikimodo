"""
Create the vb::kimodo_motion HDA via hython (no GUI needed).

Usage:
    hython scripts/create_hda.py [npz_default]

Output: vb_kimodo_motion_1.1.hda in the repo root (packed; _add_help.py expands
it into houdini/otls/).
"""

import re
import sys
from pathlib import Path

import hou

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
# _HDA_PATH is defined below, after _LIBRARY.

# Embedded geometry built by scripts/build_skin.py (run it first). When present,
# the HDA gains the skin mesh (output 0) and the A-pose skeleton (output 1).
_SKIN_BGEO = _REPO / "skin.bgeo.sc"
_APOSE_BGEO = _REPO / "apose.bgeo.sc"

# Kimodo SOMA models generate at this rate; the Source FPS parm defaults to it.
_KIMODO_FPS = 30

# Node icon (embedded into the HDA as its IconSVG section).
_ICON_SVG = _HERE / "kimodo_icon.svg"

# Node type is <namespace>::kimodo_motion::<version>; bump _VERSION for
# a breaking UI change.
_NAMESPACE = "vb"
_VERSION = "1.1"
# Library file/dir name, derived from the type name the way Houdini does it.
_LIBRARY = "%s_kimodo_motion_%s.hda" % (_NAMESPACE, _VERSION)
_HDA_PATH = _REPO / _LIBRARY


def _skin_sections():
    missing = [p for p in (_SKIN_BGEO, _APOSE_BGEO) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing embedded geometry \u2014 run `hython scripts/build_skin.py` first:\n  "
            + "\n  ".join(str(p) for p in missing)
        )
    return {
        "skin.bgeo.sc": _SKIN_BGEO.read_bytes(),
        "apose.bgeo.sc": _APOSE_BGEO.read_bytes(),
    }


_NPZ_DEFAULT = sys.argv[1] if len(sys.argv) > 1 else ""

# The SOMA77 skeleton data (joints, parents, neutral pose, T-pose rotations) is
# the single source of truth in scripts/_soma77.py; it is embedded verbatim as
# the HDA's PythonModule section and read by each cook script via
# hou.pwd().parent().type().hdaModule().
_MODULE_SRC = (_HERE / "_soma77.py").read_text(encoding="utf-8")

# Segment multiparm <-> timeline_json. The JSON stays canonical because it also
# holds the pose-key tracks, which have no sensible multiparm form; the
# multiparm is a real editor over the segments that writes back.
_MODULE_SRC += '''

import json as _json

_CLIP_CACHE = {}


def load_clip(path):
    """Every array in the NPZ, read once per file.

    The animated cook runs on every frame change (its Frame parm is $F) and an NpzFile
    re-decompresses the whole array on each lookup, so the uncached version re-read a
    couple of MB per frame to use one sample of it.
    """
    import os
    import numpy as np
    key = (path, os.path.getmtime(path))
    hit = _CLIP_CACHE.get(key)
    if hit is None:
        # ponytail: one clip cached; make it an LRU if several nodes with different
        # clips ever thrash it (that case is no slower than no cache at all).
        _CLIP_CACHE.clear()
        hit = _CLIP_CACHE[key] = dict(np.load(path))
    return hit


def read_timeline(node):
    raw = node.parm("timeline_json").eval().strip()
    try:
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}


def write_timeline(node, tl):
    """Write the JSON and the parms it mirrors. Returns True if anything changed."""
    segs = tl.get("segments") or []
    tl["version"] = 1
    tl.setdefault("transition_frames", 5)
    tl.setdefault("tracks", {})
    out = _json.dumps(tl, indent=1)
    if out == node.parm("timeline_json").eval():
        return False
    node.parm("timeline_json").set(out)
    node.parm("has_timeline").set(1 if segs else 0)
    if segs:
        node.parm("duration_frames").set(sum(int(s["frames"]) for s in segs))
    return True


def segments_from_parms(node):
    """The multiparm as a list of segment dicts."""
    out = []
    for i in range(1, int(node.parm("segments").eval()) + 1):
        out.append({"prompt": node.parm("seg_prompt%d" % i).eval(),
                    "frames": max(1, int(node.parm("seg_frames%d" % i).eval()))})
    return out


def rebuild_segments(node):
    """Push timeline_json's segments into the multiparm, without churning parms that
    already match (each set is an undo entry and a recook)."""
    segs = read_timeline(node).get("segments") or []
    p = node.parm("segments")
    if p.eval() != len(segs):
        p.set(len(segs))
    for i, s in enumerate(segs, start=1):
        pp, pf = node.parm("seg_prompt%d" % i), node.parm("seg_frames%d" % i)
        if pp.eval() != s["prompt"]:
            pp.set(s["prompt"])
        if pf.eval() != int(s["frames"]):
            pf.set(int(s["frames"]))
    refresh_starts(node, segs)


def refresh_starts(node, segs=None):
    """Fill each instance's read-only first and last scene frame. Callers that have
    already parsed the timeline pass `segs` rather than making us re-read the parm."""
    start = node.parm("start_frame")
    if start is None:                    # never take the node down over a display field
        return
    if segs is None:
        segs = read_timeline(node).get("segments") or []
    f = int(start.eval())
    for i, sg in enumerate(segs, start=1):
        a, b = node.parm("seg_from%d" % i), node.parm("seg_to%d" % i)
        if a is None or b is None:
            break
        last = f + int(sg["frames"]) - 1
        if a.eval() != f:
            a.set(f)
        if b.eval() != last:
            b.set(last)
        f = last + 1


def run_regenerate(node, index, to_end=False):
    """Shared body for the two Regenerate buttons."""
    import hou
    try:
        from kimodo_timeline import regen
    except ImportError:
        node.parm("last_error").set(
            "Regenerate needs houdini/python on PYTHONPATH (the fxhoudinikimodo package).")
        raise
    try:
        msg = regen.regenerate(node, index, to_end=to_end)
    except regen.Precondition as e:
        # A wrong turn, not a broken cook: last_error is left alone, since the
        # cook script turns it into a hou.NodeError and reddens the node.
        node.parm("status").set(str(e))
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Kimodo: %s" % e,
                                    severity=hou.severityType.Warning)
    except Exception as e:
        node.parm("last_error").set(str(e))
        node.parm("status").set("Error: %s" % e)
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Kimodo: %s" % e, severity=hou.severityType.Error)
    else:
        node.parm("last_error").set("")
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Kimodo: %s" % msg,
                                    severity=hou.severityType.ImportantMessage)


def sync_from_parms(node):
    """Multiparm edited by hand: fold it back into the JSON."""
    tl = read_timeline(node)
    new = segments_from_parms(node)
    if not new and tl.get("segments"):
        # emptied: fall back to a single prompt, keeping the first one rather than
        # losing it. This is what the old Detach Timeline button did.
        node.parm("prompt").set(tl["segments"][0]["prompt"])
    tl["segments"] = new
    changed = write_timeline(node, tl)
    refresh_starts(node, new)
    return changed
'''

_COOK_SCRIPT = r"""
import numpy as np
import hou

_m = hou.pwd().parent().type().hdaModule()
SOMA77_JOINTS  = _m.SOMA77_JOINTS
SOMA77_PARENTS = _m.SOMA77_PARENTS
TPOSE_ROTS     = _m.TPOSE_ROTS

# Joints behind each foot_contacts channel. A SOMA77 NPZ carries 6 channels, where
# ToeEnd is a copy of ToeBase (Kimodo detects contact on 2 joints per side and
# duplicates the toe channel when converting its output to SOMA77); NPZs exported
# straight from the 4-channel internal representation carry only the detected pair.
_FOOT_CHANNELS = {
    6: ("LeftFoot", "LeftToeBase", "LeftToeEnd", "RightFoot", "RightToeBase", "RightToeEnd"),
    4: ("LeftFoot", "LeftToeBase", "RightFoot", "RightToeBase"),
}

def _cook():
    node     = hou.pwd()
    hda_node = node.parent()

    # A failed Generate/Test/download leaves its message here; surface it as a node error
    # (red node, message in the node info) instead of a dialog. Cleared by the next Generate.
    err = hda_node.parm("last_error").eval().strip()
    if err:
        raise hou.NodeError(err)

    npz_path = hda_node.parm("npz_path").eval()
    if not npz_path:
        return  # no NPZ yet - output empty geometry, wait for Generate

    geo = node.geometry()
    data        = _m.load_clip(npz_path)
    posed       = data["posed_joints"]    # (T, 77, 3) world positions
    global_rots = data["global_rot_mats"] # (T, 77, 3, 3) world rotations
    T           = posed.shape[0]

    # Which clip sample to show. The clip starts on Start Frame; with Retime on, scene
    # frames are mapped onto clip samples (Source FPS / scene FPS) so a 3 s clip lasts
    # 3 s at any $FPS. Before the start the first sample holds, after the end the last.
    # ponytail: nearest sample, no blending; kinefx::motionclip + Motion Clip Retime
    # downstream if sub-frame interpolation is ever needed.
    f = hda_node.parm("frame_ref").eval() - hda_node.parm("start_frame").eval()
    if hda_node.parm("retime").eval():
        f = f * hda_node.parm("source_fps").eval() / hou.fps()
    frame = max(0, min(int(round(f)), T - 1))

    pos  = posed[frame].copy()
    grot = global_rots[frame]

    # Undo the path canonicalisation done by Generate (see the callback): rotate about Y by
    # the stored heading and translate back to the curve's first point.
    ox, oz, ang = hda_node.parmTuple("path_xform").eval()
    if ang or ox or oz:
        c, s_ = np.cos(ang), np.sin(ang)
        R = np.array([[c, 0.0, s_], [0.0, 1.0, 0.0], [-s_, 0.0, c]])   # rotation about +Y by ang
        pos = pos @ R.T + np.array([ox, 0.0, oz])
        grot = np.einsum("ij,njk->nik", R, grot)

    # Optional: Kimodo's per-frame foot-contact labels. Absent from NPZs that only
    # carry the two keys above, so the attribute is written only when they exist.
    contacts = {}
    if "foot_contacts" in data:
        fc = data["foot_contacts"]
        channels = _FOOT_CHANNELS.get(fc.shape[-1])
        if channels:
            contacts = {n: int(v) for n, v in zip(channels, fc[frame])}

    def _build_path(i):
        p = SOMA77_PARENTS[i]
        return ('/' + SOMA77_JOINTS[i]) if p < 0 else (_build_path(p) + '/' + SOMA77_JOINTS[i])
    paths = [_build_path(i) for i in range(len(SOMA77_JOINTS))]

    # Kimodo global_rot_mats are column-vector and world-axis-aligned at rest;
    # the T-pose offsets re-align each joint frame to its bone, so right-multiplying
    # gives a bone-aligned world orientation matching the T-Pose output. Houdini KineFX
    # is row-vector / row-major, so we transpose; translation goes in the last row.
    tp = np.asarray(TPOSE_ROTS, dtype=float).reshape(-1, 3, 3)
    world_rot = [grot[i] @ tp[i] for i in range(len(SOMA77_JOINTS))]
    world_m = []
    for i in range(len(SOMA77_JOINTS)):
        m = np.identity(4)
        m[:3, :3] = world_rot[i].T
        m[3, :3]  = pos[i]
        world_m.append(m)

    _ID9  = (1.,0.,0.,0.,1.,0.,0.,0.,1.)
    _ID16 = (1.,0.,0.,0., 0.,1.,0.,0., 0.,0.,1.,0., 0.,0.,0.,1.)
    geo.addAttrib(hou.attribType.Point, "name",           "")
    geo.addAttrib(hou.attribType.Point, "path",           "")
    geo.addAttrib(hou.attribType.Point, "parent_id",      -1)
    geo.addAttrib(hou.attribType.Point, "transform",      _ID9)
    geo.addAttrib(hou.attribType.Point, "localtransform", _ID16)
    if contacts:
        geo.addAttrib(hou.attribType.Point, "contact", 0)

    for i, (name, parent) in enumerate(zip(SOMA77_JOINTS, SOMA77_PARENTS)):
        # local 4x4 = world_i @ inverse(world_parent); root keeps its world matrix
        local_m = world_m[i] if parent < 0 else world_m[i] @ np.linalg.inv(world_m[parent])
        pt = geo.createPoint()
        pt.setPosition(hou.Vector3(pos[i].tolist()))
        pt.setAttribValue("name",           name)
        pt.setAttribValue("path",           paths[i])
        pt.setAttribValue("parent_id",      parent)
        pt.setAttribValue("transform",      tuple(float(v) for v in world_rot[i].T.flatten()))
        pt.setAttribValue("localtransform", tuple(float(v) for v in local_m.flatten()))
        if contacts:
            pt.setAttribValue("contact", contacts.get(name, 0))

    pts = list(geo.points())
    for i, parent in enumerate(SOMA77_PARENTS):
        if parent >= 0:
            prim = geo.createPolygon()
            prim.setIsClosed(False)
            prim.addVertex(pts[parent])
            prim.addVertex(pts[i])

_cook()
"""

_GENERATE_CB = r"""
import json, os, requests, hou
import numpy as np

node         = kwargs["node"]
url          = node.parm("server_url").eval().rstrip("/")
download_dir = node.parm("download_dir").eval()
fps          = hou.fps()
source_fps   = node.parm("source_fps").eval() or 30
start_frame  = node.parm("start_frame").eval()
retime       = bool(node.parm("retime").eval())

def fail(msg):
    # Errors live on the node (see the cook script), plus a one-line status bar note.
    node.parm("last_error").set(str(msg))
    node.parm("status").set(f"Error: {msg}")
    if hou.isUIAvailable():
        hou.ui.setStatusMessage(f"Kimodo: {msg}", severity=hou.severityType.Error)
    else:
        print("Kimodo:", msg)

node.parm("last_error").set("")

def to_sample(scene_frame):
    # Scene frame -> 0-based clip sample index, the unit Kimodo constraints use.
    f = scene_frame - start_frame
    return max(0, int(round(f * source_fps / fps))) if retime else max(0, int(f))

# Timeline (from the Kimodo Timeline panel) or the single Prompt + Duration.
timeline = None
raw_tl = node.parm("timeline_json").eval().strip()
if raw_tl:
    timeline = json.loads(raw_tl)
    if not timeline.get("segments"):
        timeline = None
if timeline:
    segments = [{"prompt": s["prompt"].strip(), "duration": s["frames"] / fps} for s in timeline["segments"]]
    duration_s = sum(s["duration"] for s in segments)
else:
    segments = None
    duration_s = node.parm("duration_frames").eval() / fps   # scene frames -> seconds

try:
    if segments and any(not s["prompt"] for s in segments):
        raise ValueError("Every timeline segment needs a prompt.")
    # Optional Kimodo constraints: the inline JSON wins; otherwise read the file.
    raw = node.parm("constraints_json").eval().strip()
    if not raw:
        cfile = node.parm("constraints_file").eval().strip()
        raw = open(cfile, encoding="utf-8").read() if cfile else ""
    constraints = json.loads(raw) if raw else None
    if constraints is not None and not isinstance(constraints, list):
        raise ValueError("Constraints must be a JSON list of constraint dicts.")

    # Optional input geometry -> a root2d constraint. Houdini world XZ maps 1:1 to
    # Kimodo's smooth_root_2d (same Y-up metric space the node outputs). Points with
    # a `frame` int attribute become sparse waypoints (you control the timing);
    # otherwise the points (in order, e.g. a polyline) are spread evenly over the
    # clip as a denser path.
    ins = node.inputs()
    node.parmTuple("path_xform").set((0.0, 0.0, 0.0))
    if ins and ins[0] is not None:
        geo = node.inputGeometry(0)
        pts = geo.points()
        if pts:
            xz = [[float(p.position()[0]), float(p.position()[2])] for p in pts]
            # Thin a dense path to Path Waypoints points by arc length (keeping first and
            # last). A waypoint every frame or two pins the pelvis to a perfectly smooth
            # constant-speed glide and the body floats along the curve; a handful of anchors
            # lets the model put its own stride rhythm back between them.
            def thin(points, nwp):
                if nwp < 2 or len(points) <= nwp:
                    return list(range(len(points)))
                import math
                cum = [0.0]
                for a_, b_ in zip(points, points[1:]):
                    cum.append(cum[-1] + math.hypot(b_[0] - a_[0], b_[1] - a_[1]))
                total_len = cum[-1] or 1.0
                keep, j = [], 0
                for k in range(nwp):
                    target = total_len * k / (nwp - 1)
                    while j < len(cum) - 1 and cum[j + 1] < target:
                        j += 1
                    keep.append(j if k < nwp - 1 else len(points) - 1)
                return sorted(set(keep))
            nwp = int(node.parm("path_waypoints").eval())
            if geo.findPointAttrib("frame") is not None:
                # Artist-timed waypoints (scene frames -> clip samples), thinned the same way.
                idx = thin(xz, nwp)
                frames = [to_sample(int(pts[i].attribValue("frame"))) for i in idx]
                xz = [xz[i] for i in idx]
            elif len(pts) > 1:
                idx = thin(xz, nwp)
                xz = [xz[i] for i in idx]
                T = max(2, int(duration_s * source_fps))   # clip samples, not scene frames
                frames = [int(round(i * (T - 1) / (len(xz) - 1))) for i in range(len(xz))]
            else:
                frames = [0]
            # dedup by frame (keep first), sort by frame
            by_frame = {}
            for f, c in zip(frames, xz):
                by_frame.setdefault(f, c)
            items = sorted(by_frame.items())
            pts_xz = [c for _, c in items]
            # Canonicalise: Kimodo generates with the root at XZ (0,0) facing +Z on the first
            # sample. A path that starts elsewhere or heads off-axis forces a lurch/turn in the
            # first frames. Translate the first waypoint to the origin and rotate the path so its
            # initial tangent is +Z; the cook applies the inverse so the result sits on the curve.
            import math
            ox, oz = pts_xz[0]
            dx, dz = next(((x - ox, z - oz) for x, z in pts_xz[1:] if abs(x - ox) + abs(z - oz) > 1e-4), (0.0, 1.0))
            ang = math.atan2(dx, dz)                      # heading of the path start, from +Z toward +X
            ca, sa = math.cos(-ang), math.sin(-ang)
            canon = [[(x - ox) * ca + (z - oz) * sa, -(x - ox) * sa + (z - oz) * ca] for x, z in pts_xz]
            node.parmTuple("path_xform").set((ox, oz, ang))
            root2d = {"type": "root2d",
                      "frame_indices": [f for f, _ in items],
                      "smooth_root_2d": canon}
            constraints = (constraints or []) + [root2d]

    # Optional posed-skeleton input (input 1) -> a full-body / end-effector constraint.
    # Pose the node's own Capture Pose (A-pose) output with KineFX, keyframe it, and wire
    # it back here. At each listed keyframe we read the posed joints and invert the cook's
    # forward transform to recover Kimodo's global rotation: the cook stores
    # transform = (grot @ tp).T, so grot = (transform.T) @ tp.T  (tp = TPOSE_ROTS).
    # Positions are already Kimodo-global. The server builds the constraint via the
    # FullBody/EndEffector constructor (same path Kimodo's demo uses).
    def sample_pose(scene_frames):
        # Read the posed SOMA77 skeleton on input 1 at each scene frame.
        m = node.type().hdaModule()
        joints = list(m.SOMA77_JOINTS)
        tp = np.asarray(m.TPOSE_ROTS, dtype=float).reshape(-1, 3, 3)
        idx = {n: i for i, n in enumerate(joints)}
        src = ins[1]
        pos_kf, rot_kf, sr_kf = [], [], []
        for f in scene_frames:
            g = src.geometryAtFrame(f)
            if g.findPointAttrib("name") is None:
                raise ValueError("Pose input must be a SOMA77 skeleton with a `name` point attribute.")
            P = [None] * len(joints)
            R = [None] * len(joints)
            for pt in g.points():
                nm = pt.attribValue("name")
                if nm not in idx:
                    continue
                i = idx[nm]
                P[i] = [float(v) for v in pt.position()]
                world = np.array(pt.attribValue("transform"), dtype=float).reshape(3, 3).T
                R[i] = (world @ tp[i].T).tolist()
            if any(p is None for p in P):
                missing = [joints[i] for i, p in enumerate(P) if p is None]
                raise ValueError("Pose input missing SOMA77 joints: %s" % ", ".join(missing[:5]))
            pos_kf.append(P)
            rot_kf.append(R)
            hips = P[idx["Hips"]]
            sr_kf.append([hips[0], hips[2]])
        return {"frame_indices": [to_sample(f) for f in scene_frames],
                "global_joints_positions": pos_kf,
                "global_joints_rots": rot_kf,
                "smooth_root_2d": sr_kf}

    has_pose_input = len(ins) > 1 and ins[1] is not None
    if timeline:
        # One constraint per non-empty track: fullbody pins everything, a limb track pins
        # that joint only (ee-global with a single joint name).
        tracks = {t: sorted(int(k) for k in ks) for t, ks in (timeline.get("tracks") or {}).items() if ks}
        if tracks and not has_pose_input:
            raise ValueError("The timeline has pose keys but nothing is wired to input 1 (posed skeleton).")
        for track, keys in tracks.items():
            cdict = sample_pose(keys)
            if track == "fullbody":
                cdict["type"] = "fullbody-global"
            else:
                cdict["type"] = "ee-global"
                cdict["joint_names"] = [track]
            constraints = (constraints or []) + [cdict]
    else:
        kf = node.parm("pose_keyframes").eval().strip()
        if has_pose_input and kf:
            frames = [int(x) for x in kf.replace(",", " ").split()]
            cdict = sample_pose(frames)
            if node.parm("pose_type").evalAsString() == "End-Effector":
                names = [jn for parm, jn in (("ee_left_hand", "LeftHand"), ("ee_right_hand", "RightHand"),
                                             ("ee_left_foot", "LeftFoot"), ("ee_right_foot", "RightFoot"))
                         if node.parm(parm).eval()]
                if not names:
                    raise ValueError("End-Effector pose constraint: select at least one joint (hand/foot).")
                cdict["type"] = "ee-global"
                cdict["joint_names"] = names
            else:
                cdict["type"] = "fullbody-global"
            constraints = (constraints or []) + [cdict]

    payload = {
        "model":       node.parm("model").evalAsString(),
        "force":       bool(node.parm("force").eval()),
        "constraints": constraints,
    }
    if segments:
        payload["segments"] = segments
        payload["transition_frames"] = int(timeline.get("transition_frames", 5))
    else:
        payload["prompt"] = node.parm("prompt").eval().strip()
        payload["duration"] = duration_s
    resp = requests.post(f"{url}/generate", json=payload, timeout=30)
    if resp.status_code == 422 and segments:
        raise ValueError("Server rejected the timeline request; it may predate multi-prompt support. "
                         "Update kimodo_server.py and restart the api container.\n" + resp.text[:300])
    resp.raise_for_status()
except Exception as e:
    fail(e)
else:
    job_id = resp.json()["job_id"]
    node.parm("job_id").set(job_id)
    node.parm("progress").set(0.0)
    node.parm("status").set(f"Queued ({job_id[:8]}...)")

    def _finish(data, suffix):
        # Runs on the main thread from the event-loop callback, so plain HOM calls are
        # fine here: no thread marshalling, no deferred eval.
        frames, joints = data["frames"], data["joints"]
        try:
            node.parm("status").set(f"Downloading...{suffix}")
            os.makedirs(download_dir, exist_ok=True)
            # forward slashes: Houdini's own convention, and what the user typed
            local_npz = download_dir.rstrip("/\\") + f"/{job_id}.npz"
            with requests.get(f"{url}/jobs/{job_id}/download", timeout=120, stream=True) as dl:
                dl.raise_for_status()
                with open(local_npz, "wb") as fh:
                    for chunk in dl.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
        except Exception as e:
            fail(f"NPZ download failed: {e}")
            node.parm("job_id").set("")
            return
        src_fps = node.parm("source_fps").eval() or 30
        secs = frames / src_fps
        node.parm("status").set(f"Done{suffix}" + (" (cached)" if data.get("cached") else ""))
        node.parm("progress").set(1.0)
        node.parm("clip_info").set(
            f"{secs:.2f} s = {round(secs * hou.fps())} frames @ {hou.fps():g} fps "
            f"({frames} samples @ {src_fps:g} fps)")
        node.parm("npz_path").set(local_npz)
        node.parm("job_id").set("")
        node.cook(force=True)
        if hou.isUIAvailable():
            hou.ui.setStatusMessage(
                f"Kimodo: generated {frames} frames ({joints} joints){suffix}.",
                severity=hou.severityType.ImportantMessage)

    # Non-blocking, the way SideFX polls (hdefereval is built on the same callback):
    # Houdini stays interactive, progress lands on the node's parms, and the Timeline
    # panel's bar renders it. No sleeping on the main thread, no modal dialog, no
    # background thread marshalling HOM calls back with hdefereval.
    try:
        from kimodo_timeline.poller import JobWatcher
    except ImportError:
        fail("Generate needs houdini/python on PYTHONPATH (the fxhoudinikimodo package).")
    else:
        JobWatcher(node, url, job_id, "Running", _finish).start()
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Kimodo: generation started, watch the node's Status field.",
                                    severity=hou.severityType.ImportantMessage)
"""

# Cancel is identical for both HDAs (same server endpoint).
_CANCEL_CB = r"""
import requests, hou

node   = kwargs["node"]
url    = node.parm("server_url").eval().rstrip("/")
job_id = node.parm("job_id").eval()
if not job_id:
    hou.ui.setStatusMessage("Kimodo: no active job to cancel.", severity=hou.severityType.Warning)
else:
    try:
        r = requests.post(f"{url}/jobs/{job_id}/cancel", timeout=10)
        r.raise_for_status()
    except Exception as e:
        node.parm("status").set(f"Cancel failed: {e}")
        hou.ui.setStatusMessage(f"Kimodo: cancel failed: {e}", severity=hou.severityType.Error)
    else:
        node.parm("status").set("Cancelled")
        node.parm("job_id").set("")
"""

# Test Connection: one GET on /health, result into Status and the status bar.
_TEST_CB = r"""
import requests, hou

node = kwargs["node"]
url  = node.parm("server_url").eval().rstrip("/")
try:
    r = requests.get(f"{url}/health", timeout=5)
    r.raise_for_status()
    d = r.json()
    msg = "Server OK" + (" (mock mode, no inference)" if d.get("mock_mode") else "")
    sev = hou.severityType.ImportantMessage
except Exception as e:
    msg = f"Server unreachable: {e}"
    sev = hou.severityType.Error
node.parm("status").set(msg)
hou.ui.setStatusMessage("Kimodo: " + msg, severity=sev)
"""

# Build a standalone A-pose rig to pose for full-body / end-effector
# constraints. It loads the HDA's embedded apose section by *type name*, so it's
# independent of this node's outputs - no output-into-its-own-input loop. Wired
# into input 1 ready to pose.
_MAKE_RIG_CB = r"""
import hou

node = kwargs["node"]
parent = node.parent()
loader = (
    "import os, base64, tempfile, hou\n"
    "hda = hou.nodeType(hou.sopNodeTypeCategory(), %r).definition()\n"
    "data = base64.b64decode(hda.sections()['apose.bgeo.sc'].contents())\n"
    "fd, p = tempfile.mkstemp(prefix='kimodo_', suffix='_apose.bgeo.sc'); os.close(fd)\n"
    "try:\n"
    "    open(p, 'wb').write(data)\n"
    "    hou.pwd().geometry().loadFromFile(p)\n"
    "finally:\n"
    "    try: os.remove(p)\n"
    "    except OSError: pass\n"
) % node.type().name()

rig = parent.createNode("python", "kimodo_pose_rig")
rig.parm("python").set(loader)
try:
    tip = parent.createNode("kinefx::rigpose", "pose_keyframes")
    tip.setInput(0, rig)
except hou.OperationFailed:
    tip = rig
rig.moveToGoodPosition()
if tip is not rig:
    tip.moveToGoodPosition()
wired = node.input(1) is None
if wired:
    node.setInput(1, tip)
tip.setCurrent(True, clear_all_selected=True)
hou.ui.setStatusMessage(
    "Kimodo: created an A-pose rig%s%s. Pose and keyframe it, then add pose keys (Timeline) or Pose Keyframes." % (
        " + Rig Pose" if tip is not rig else "",
        " wired to input 1" if wired else "; wire it into this node's input 1",
    ),
    severity=hou.severityType.ImportantMessage,
)
"""

_REST_SCRIPT = r"""
import hou

_err = hou.pwd().parent().parm("last_error").eval().strip()
if _err:
    raise hou.NodeError(_err)   # failed Generate: every output flags it, see the cook script
_m = hou.pwd().parent().type().hdaModule()
SOMA77_JOINTS  = _m.SOMA77_JOINTS
SOMA77_PARENTS = _m.SOMA77_PARENTS
TPOSE_ROTS     = _m.TPOSE_ROTS
NEUTRAL_JOINTS = _m.NEUTRAL_JOINTS
_ID9 = (1.,0.,0.,0.,1.,0.,0.,0.,1.)
def _t3(m):  # transpose a flat row-major 3x3 (Kimodo column-vector -> Houdini row-vector)
    return (m[0],m[3],m[6], m[1],m[4],m[7], m[2],m[5],m[8])
geo = hou.pwd().geometry()
geo.addAttrib(hou.attribType.Point, "name",      "")
geo.addAttrib(hou.attribType.Point, "transform", _ID9)
for i, name in enumerate(SOMA77_JOINTS):
    pt = geo.createPoint()
    pt.setPosition(hou.Vector3(NEUTRAL_JOINTS[i]))
    pt.setAttribValue("name",      name)
    pt.setAttribValue("transform", _t3(TPOSE_ROTS[i]))
pts = list(geo.points())
for i, parent in enumerate(SOMA77_PARENTS):
    if parent >= 0:
        prim = geo.createPolygon()
        prim.setIsClosed(False)
        prim.addVertex(pts[parent])
        prim.addVertex(pts[i])
"""

# Cook script for an output that loads geometry embedded in the HDA as a section
# (used for the A-pose skeleton and the skin mesh). Reads the section bytes from
# the HDA definition, writes them to a temp file, and loads them - avoids opdef:
# path resolution and keeps the HDA self-contained.
_SECTION_LOADER = """import os, base64, tempfile, hou
node = hou.pwd()
_err = node.parent().parm("last_error").eval().strip()
if _err:
    raise hou.NodeError(_err)   # failed Generate: every output flags it, see the cook script
name = "%s"
raw = node.parent().type().definition().sections()[name].contents()
data = base64.b64decode(raw)
fd, path = tempfile.mkstemp(prefix="kimodo_", suffix="_" + name)
os.close(fd)
try:
    with open(path, "wb") as fh:
        fh.write(data)
    node.geometry().loadFromFile(path)
finally:
    try:
        os.remove(path)
    except OSError:
        pass
"""

# Open Timeline: focus an existing Kimodo Timeline pane tab or float a new one,
# and select this node so the panel picks it up.
_OPEN_TIMELINE_CB = r"""
import hou
node = kwargs["node"]
node.setSelected(True, clear_all_selected=True)
desk = hou.ui.curDesktop()
tab = None
for pt in hou.ui.paneTabs():          # hou.ui.paneTabs() includes floating panels; desk.paneTabs() does not
    if pt.type() == hou.paneTabType.PythonPanel:
        iface = pt.activeInterface()
        if iface is not None and iface.name() == "kimodo_timeline":
            tab = pt
            break
if tab is None:
    iface = hou.pypanel.interfaceByName("kimodo_timeline")
    if iface is None:
        node.parm("last_error").set("Kimodo Timeline panel not found: is houdini/python_panels on HOUDINI_PATH (fxhoudinikimodo package)?")
        hou.ui.setStatusMessage("Kimodo: Timeline panel not found (see node error).", severity=hou.severityType.Error)
    else:
        tab = desk.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 420))
        tab.setActiveInterface(iface)
if tab is not None:
    tab.setIsCurrentTab()
"""

# The panel picks any change up from timeline_json on its next tick.
_SEG_SYNC_CB = r"""
node = kwargs["node"]
node.type().hdaModule().sync_from_parms(node)
"""


_SEG_SPLIT_CB = r"""
import hou
node = kwargs["node"]
i = int(kwargs["script_multiparm_index"]) - 1
m = node.type().hdaModule()
tl = m.read_timeline(node)
segs = tl.get("segments") or []
if 0 <= i < len(segs):
    start = int(node.parm("start_frame").eval()) + sum(int(s["frames"]) for s in segs[:i])
    left = int(round(hou.frame())) - start
    if 0 < left < int(segs[i]["frames"]):
        segs.insert(i + 1, {"prompt": segs[i]["prompt"],
                            "frames": int(segs[i]["frames"]) - left})
        segs[i]["frames"] = left
        tl["segments"] = segs
        with hou.undos.group("Kimodo: split segment"):
            m.write_timeline(node, tl)
            m.rebuild_segments(node)
    elif hou.isUIAvailable():
        hou.ui.setStatusMessage(
            "Kimodo: put the playhead inside this segment to split it.",
            severity=hou.severityType.Warning)
"""


_SEG_REGEN_CB = r"""
node = kwargs["node"]
node.type().hdaModule().run_regenerate(node, int(kwargs["script_multiparm_index"]) - 1, False)
"""


_SEG_REGEN_END_CB = r"""
node = kwargs["node"]
node.type().hdaModule().run_regenerate(node, int(kwargs["script_multiparm_index"]) - 1, True)
"""


# Runs when a node of this type is created: node shape, and the first segment.
_ON_CREATED = r"""
node = kwargs["node"]
node.setUserData("nodeshape", "bulge")
# Seed the first segment from the defaults. Keyed on the timeline being empty, not on
# the instance count: the multiparm defaults to 1, so a count test never fires.
if not node.parm("timeline_json").eval().strip():
    if node.parm("segments").eval() < 1:
        node.parm("segments").set(1)
    if not node.parm("seg_prompt1").eval():
        node.parm("seg_prompt1").set(node.parm("prompt").eval())
        node.parm("seg_frames1").set(int(node.parm("duration_frames").eval()))
    node.type().hdaModule().sync_from_parms(node)
"""

_PROMPT_HELP = (
    "What the character does, in plain __English__. Be specific about body part, "
    "direction, speed and style.\n\n"
    "__Name the body mechanics, not the intent.__ This matters more than any parameter "
    "on this node. Measured: _a person jumps forward and lands on both feet_ gets both "
    "feet 0.22 off the ground; _a person leaps high into the air with both feet off the "
    "ground_ gets 0.94. Same model, same duration, 4.3x the result.\n\n"
    "__Examples__\n"
    "_a person walks forward slowly_\n"
    "_someone jogs in a circle then stops_\n"
    "_a person leaps high into the air with both feet off the ground_\n"
    "_a person squats down deeply, knees bent, hips near the floor_\n\n"
    "__Do not prompt for finger or hand detail.__ Kimodo predicts on a 30-joint skeleton "
    "that strips hand detail; the fingers you get back are reconstructed, never generated. "
    "Set __Blend Fingers__ to 0 when retargeting, for the same reason."
)


def build_hda(node_name, description, hda_path, generate_cb):
    """Build the kimodo_motion HDA.

    Output order follows SideFX's character/test-geometry nodes so a Joint
    Deform wires straight across (0 -> 0, 1 -> 1, 2 -> 2):
      0 Rest Geometry (skin mesh)   1 Capture Pose (A-pose)
      2 Animated Pose               3 T-Pose
    """
    skin_sections = _skin_sections()  # {section_name: bytes} from build_skin.py
    obj = hou.node("/obj")
    geo = obj.createNode("geo", node_name + "_setup")
    geo.deleteItems(geo.children())

    subnet = geo.createNode("subnet", node_name + "_subnet")

    # Both cook scripts read the SOMA77 data from the HDA's PythonModule section
    # (added below) via hou.pwd().parent().type().hdaModule() - single source.
    anim_sop = subnet.createNode("python", "animated_sop")
    anim_sop.parm("python").set(_COOK_SCRIPT)
    tpose_sop = subnet.createNode("python", "tpose_sop")
    tpose_sop.parm("python").set(_REST_SCRIPT)

    # Houdini takes each output connector's colour from its internal Output SOP,
    # so these match kinefx::characterio::2.0 and read the same way in the
    # network editor. Index 1 (Capture Pose) is SideFX's default grey, so it is
    # left alone.
    OUT_COLORS = {
        0: (0.584, 0.776, 1.0),  # Rest Geometry  - light blue
        2: (0.976, 0.780, 0.263),
    }  # Animated Pose  - amber

    def _out(idx, src):
        o = subnet.createNode("output", "output%d" % idx)
        o.setInput(0, src)
        o.parm("outputidx").set(idx)
        if idx in OUT_COLORS:
            o.setColor(hou.Color(OUT_COLORS[idx]))
        return o

    skin_sop = subnet.createNode("python", "skin_sop")
    skin_sop.parm("python").set(_SECTION_LOADER % "skin.bgeo.sc")
    apose_sop = subnet.createNode("python", "apose_sop")
    apose_sop.parm("python").set(_SECTION_LOADER % "apose.bgeo.sc")
    first = _out(0, skin_sop)
    _out(1, apose_sop)
    _out(2, anim_sop)
    _out(3, tpose_sop)
    labels = ["Rest Geometry", "Capture Pose", "Animated Pose", "T-Pose"]

    first.setDisplayFlag(True)
    first.setRenderFlag(True)
    subnet.layoutChildren()

    # createDigitalAsset appends to an existing library file; start clean so the
    # packed file holds exactly one definition (the one _add_help.py picks up).
    if hda_path.exists():
        hda_path.unlink()
    hda_node = subnet.createDigitalAsset(
        name="%s::%s::%s"
        % (_NAMESPACE, node_name, _VERSION),  # version must be in the name
        hda_file_name=str(hda_path),
        description=description,
        min_num_inputs=0,
        # input 0: geometry -> root2d. input 1: posed skeleton -> fullbody/EE.
        max_num_inputs=2,
        version=_VERSION,
    )
    hda_def = hda_node.type().definition()
    hda_def.setMaxNumOutputs(len(labels))
    # Node icon: the SVG in scripts/kimodo_icon.svg, embedded as the IconSVG
    # section. Namespaced opdef form:
    # opdef:/<namespace>::Sop/<name>::<version>?IconSVG
    scope, ns, base, ver = hda_node.type().nameComponents()
    icon_path = "opdef:/%sSop/%s%s?IconSVG" % (
        ns + "::" if ns else "",
        base,
        "::" + ver if ver else "",
    )
    hda_def.addSection("IconSVG", _ICON_SVG.read_text(encoding="utf-8"))
    hda_def.setIcon(icon_path)
    hda_def.addSection(
        "PythonModule", _MODULE_SRC
    )  # SOMA77 data for the cook scripts
    hda_def.addSection("OnCreated", _ON_CREATED)
    hda_def.setExtraFileOption("OnCreated/IsPython", True)
    # Shown under the node in the network editor (Type Properties > Node >
    # Descriptive Parm).
    hda_def.addSection("DescriptiveParmName", "status")
    # Store the binary bgeo as base64 text so the section round-trips cleanly
    # (HDASection.contents() returns str; raw bytes don't survive that).
    import base64

    for sname, data in skin_sections.items():
        hda_def.addSection(sname, base64.b64encode(data).decode("ascii"))

    # Tab menu: Kimodo instead of the generic "Digital Assets" submenu.
    shelf = hda_def.sections().get("Tools.shelf")
    if shelf is not None:
        xml = re.sub(
            r"<toolSubmenu>.*?</toolSubmenu>",
            "<toolSubmenu>Kimodo</toolSubmenu>",
            shelf.contents(),
            count=1,
        )
        hda_def.addSection("Tools.shelf", xml)

    ###### Parameter interface
    ptg = hou.ParmTemplateGroup()  # start fresh - no inherited subnet parms
    # The Timeline panel drives these while it has data.
    timeline_owns = "{ has_timeline == 1 }"

    # Tab: Generate - the everyday controls.
    gen = hou.FolderParmTemplate(
        "fld_generate", "Generate", folder_type=hou.folderType.Tabs
    )
    gen.addParmTemplate(
        hou.ButtonParmTemplate(
            "open_timeline",
            "Open Timeline",
            script_callback=_OPEN_TIMELINE_CB,
            script_callback_language=hou.scriptLanguage.Python,
            is_label_hidden=True,
            join_with_next=True,
            help="Open the __Kimodo Timeline__ panel for this node: prompt segments laid end to "
            "end, transitions, and Full Body / hand / foot pose tracks.\nWhile a timeline "
            "exists it owns Prompt, Duration and the pose parameters below.\n\n"
            "__A segment is weaker in a timeline than on its own.__ A segment spends its "
            "opening transitioning out of the previous motion, so it has less time left for "
            "its own action. Measured: _a person stands up and turns around_ turns 173 "
            "deg generated alone, but only 18 deg as segment 6 of 8. Generate a segment on its "
            "own first to check it works, then add it to the timeline.\n"
            "If a segment comes out weak, __split it__ rather than lengthen it: as one "
            "2.25 s segment that turn managed 64 deg, at 3.50 s it managed 154 deg, but split "
            "into _stands up from a squat_ + _turns around to face the opposite "
            "direction_ it managed 193 deg for the same total time.",
        )
    )
    gen.addParmTemplate(
        hou.ButtonParmTemplate(
            "generate",
            "Generate",
            script_callback=generate_cb,
            script_callback_language=hou.scriptLanguage.Python,
            is_label_hidden=True,
            join_with_next=True,
            help="Send the prompt to the server. Houdini stays interactive; the node recooks "
            "when the clip has downloaded.\nProgress shows in __Status__, under the node, "
            "and as a bar in the Kimodo Timeline panel. __Cancel__ stops it.",
        )
    )
    gen.addParmTemplate(
        hou.ButtonParmTemplate(
            "cancel",
            "Cancel",
            script_callback=_CANCEL_CB,
            script_callback_language=hou.scriptLanguage.Python,
            is_label_hidden=True,
            help="Cancel the queued job or discard the running one.",
        )
    )
    gen.addParmTemplate(
        hou.MenuParmTemplate(
            "model",
            "Model",
            (
                "Kimodo-SOMA-RP-v1.1",
                "Kimodo-SOMA-SEED-v1.1",
                "Kimodo-SOMA-RP-v1",
            ),
            default_value=0,
            help="Kimodo checkpoint, named _Family-Skeleton-Dataset-version_.\n"
            "__RP__ = Bones Rigplay 1 (~700 h of mocap), the recommended default.\n"
            "__SEED__ = BONES-SEED (288 h, public data), weaker but the benchmark's reference.",
        )
    )
    gen.addParmTemplate(
        hou.ToggleParmTemplate(
            "force",
            "Force Regenerate",
            default_value=True,
            help="Bypass the server cache and run inference again even if an identical "
            "_prompt + duration + model + constraints_ was generated before.",
        )
    )
    seg = hou.FolderParmTemplate(
        "segments",
        "Segments",
        folder_type=hou.folderType.ScrollingMultiparmBlock,
    )
    seg.setDefaultValue(1)  # a node always has at least one segment
    seg.addParmTemplate(
        hou.StringParmTemplate(
            "seg_prompt#",
            "Prompt",
            1,
            default_value=("",),
            script_callback=_SEG_SYNC_CB,
            script_callback_language=hou.scriptLanguage.Python,
            help=_PROMPT_HELP,
        )
    )
    seg.addParmTemplate(
        hou.IntParmTemplate(
            "seg_from#",
            "Frames",
            1,
            default_value=(0,),
            is_hidden=True,
            help="First scene frame of this segment, counted from __Start Frame__.",
        )
    )
    seg.addParmTemplate(
        hou.IntParmTemplate(
            "seg_to#",
            "to",
            1,
            default_value=(0,),
            is_hidden=True,
            help="Last scene frame of this segment.",
        )
    )
    seg.addParmTemplate(
        hou.LabelParmTemplate(
            "seg_range#",
            "Frames",
            join_with_next=True,
            # sixteen columns like Status: one wide column centres the text
            column_labels=(
                '`chs("seg_from#")` - `chs("seg_to#")`   '
                '(`rint(ch("seg_frames#") / ch("scene_fps") * 100) / 100` s)',
            )
            + ("",) * 15,
            help="Scene frames this segment occupies, counted from __Start Frame__. "
            "Read-only: it follows the lengths above it.",
        )
    )
    seg.addParmTemplate(
        hou.IntParmTemplate(
            "seg_frames#",
            "Length",
            1,
            default_value=(48,),
            min=1,
            max=240,
            min_is_strict=True,
            max_is_strict=False,
            join_with_next=True,
            script_callback=_SEG_SYNC_CB,
            script_callback_language=hou.scriptLanguage.Python,
            help="Length of this segment in scene frames.",
        )
    )
    seg.addParmTemplate(
        hou.ButtonParmTemplate(
            "seg_split#",
            "Split",
            script_callback=_SEG_SPLIT_CB,
            script_callback_language=hou.scriptLanguage.Python,
            join_with_next=True,
            help="Cut this segment in two at the playhead, keeping the prompt on both halves.",
        )
    )
    seg.addParmTemplate(
        hou.ButtonParmTemplate(
            "seg_regen#",
            "Regenerate",
            script_callback=_SEG_REGEN_CB,
            script_callback_language=hou.scriptLanguage.Python,
            join_with_next=True,
            help="Re-roll this segment alone. Everything before and after it is untouched and "
            "the clip keeps its length.\n"
            "The previous segment's tail is sent back as the seam and this segment's own "
            "tail is pinned to the frames the next one was generated against, so both joins "
            "stay continuous; measured at 0.80x and 0.39x of the clip's own per-sample motion.\n"
            "Much cheaper than Generate, which re-runs the whole clip. Blocks until done.\n"
            "Not available on the first segment, which has no earlier motion to continue from.",
        )
    )
    seg.addParmTemplate(
        hou.ButtonParmTemplate(
            "seg_regen_end#",
            "From Here",
            script_callback=_SEG_REGEN_END_CB,
            script_callback_language=hou.scriptLanguage.Python,
            help="Re-roll this segment and every segment after it, keeping everything before it. Use this when the change should carry through the rest of the clip; use __Regenerate__ when only this segment is wrong.",
        )
    )
    gen.addParmTemplate(seg)
    gen.addParmTemplate(
        hou.StringParmTemplate(
            "prompt",
            "Prompt",
            1,
            default_value=("a person walks forward",),
            tags={"editor": "1", "editorlines": "4-8"},
            is_hidden=True,
            help="Legacy single prompt, kept so HIPs saved before the Segments multiparm "
            "still read. The __Segments__ above are what gets generated.",
        )
    )
    gen.addParmTemplate(
        hou.IntParmTemplate(
            "duration_frames",
            "Duration (frames)",
            1,
            default_value=(72,),
            min=12,
            max=720,
            min_is_strict=False,
            max_is_strict=False,
            is_hidden=True,
            help="Length of the clip in __scene frames__ at the current `$FPS` "
            "(`72` = 3 s at 24 fps).\nConverted to seconds for Kimodo, which "
            "generates at 30 fps; with __Retime to Scene FPS__ on you get back exactly this "
            "many frames.",
        )
    )
    gen.addParmTemplate(
        hou.StringParmTemplate(
            # Read by the Timeline panel, and shown under the node via
            # DescriptiveParmName.
            "status",
            "Status",
            1,
            default_value=("",),
            is_hidden=True,
        )
    )
    gen.addParmTemplate(
        hou.StringParmTemplate(
            # Read by the Timeline panel's footer.
            "clip_info",
            "Clip",
            1,
            default_value=("",),
            is_hidden=True,
        )
    )
    gen.addParmTemplate(
        hou.FloatParmTemplate(
            "scene_fps",
            "Scene FPS",
            1,
            default_expression=("$FPS",),
            default_expression_language=(hou.scriptLanguage.Hscript,),
            is_hidden=True,
            help="The scene FPS, as an expression, so the segment lengths in seconds follow "
            "it without anything having to refresh them.",
        )
    )
    ptg.append(gen)

    # Tab: Constraints - optional steering.
    con = hou.FolderParmTemplate(
        "fld_constraints", "Constraints", folder_type=hou.folderType.Tabs
    )
    # Collapsible groups instead of separators; group_default 1 = open, 0 =
    # closed on creation.
    path = hou.FolderParmTemplate(
        "grp_path",
        "Root Path (input 0)",
        folder_type=hou.folderType.Collapsible,
        tags={"group_default": "1"},
    )
    path.addParmTemplate(
        hou.IntParmTemplate(
            "path_waypoints",
            "Path Waypoints",
            1,
            default_value=(8,),
            min=0,
            max=64,
            min_is_strict=True,
            max_is_strict=False,
            help="A curve or points on input 0 become a __root2d__ constraint: the root passes "
            "through these XZ positions.\nWithout a `frame` attribute the curve is "
            "thinned to this many points by arc length and spread evenly over the clip, so the "
            "model keeps room to slow down or stop between them. `0` = use every "
            "point (constant speed along the whole clip, which fights segments that should "
            "stand still or fall, and a pelvis that glides instead of stepping).\nPoints "
            "with an `int frame` attribute are waypoints at those scene frames, "
            "thinned the same way: use them to constrain only the part of the clip that "
            "should travel.",
        )
    )
    con.addParmTemplate(path)
    js = hou.FolderParmTemplate(
        "grp_json",
        "Constraints JSON",
        folder_type=hou.folderType.Collapsible,
        tags={"group_default": "0"},
    )
    js.addParmTemplate(
        hou.StringParmTemplate(
            "constraints_file",
            "Constraints File",
            1,
            default_value=("",),
            string_type=hou.stringParmType.FileReference,
            file_type=hou.fileType.Any,
            tags={"filechooser_pattern": "*.json"},
            help="Optional Kimodo constraints JSON (e.g. exported from the Kimodo demo). "
            "Ignored when Constraints JSON below is non-empty.",
        )
    )
    js.addParmTemplate(
        hou.StringParmTemplate(
            "constraints_json",
            "Constraints JSON",
            1,
            default_value=("",),
            tags={"editor": "1", "editorlines": "3-8"},
            help="Optional inline Kimodo constraints JSON (a list of constraint dicts). "
            "Takes precedence over __Constraints File__.\n\n__Example root path__\n"
            '`[{"type": "root2d", "frame_indices": [0, 90], '
            '"smooth_root_2d": [[0,0],[2,1]]}]`',
        )
    )
    con.addParmTemplate(js)
    pose = hou.FolderParmTemplate(
        "grp_pose",
        "Pose Keyframes (input 1)",
        folder_type=hou.folderType.Collapsible,
        tags={"group_default": "1"},
    )
    pose.addParmTemplate(
        hou.ButtonParmTemplate(
            "make_pose_rig",
            "Create Pose Rig",
            script_callback=_MAKE_RIG_CB,
            script_callback_language=hou.scriptLanguage.Python,
            help="Drop an independent A-pose rig (+ Rig Pose) into the network and wire it to "
            "input 1. Pose / keyframe it to author full-body / end-effector constraints.",
        )
    )
    pose.addParmTemplate(
        hou.StringParmTemplate(
            "pose_keyframes",
            "Pose Keyframes",
            1,
            default_value=("",),
            disable_when=timeline_owns,
            help="Frame numbers to sample the input-1 skeleton at, e.g. `0 45 89`.\n"
            "Empty = no pose constraint. Disabled while the Timeline panel owns the keys.",
        )
    )
    pose.addParmTemplate(
        hou.MenuParmTemplate(
            "pose_type",
            "Pose Constraint",
            ("Full-Body", "End-Effector"),
            default_value=0,
            disable_when='{ pose_keyframes == "" } ' + timeline_owns,
            help="How to use the posed skeleton on input 1: constrain the whole body, "
            "or only the selected hands/feet.",
        )
    )
    pose_ee = (
        '{ pose_type != "End-Effector" } { pose_keyframes == "" } '
        + timeline_owns
    )
    pose.addParmTemplate(
        hou.ToggleParmTemplate(
            "ee_left_hand",
            "Left Hand",
            default_value=False,
            disable_when=pose_ee,
            join_with_next=True,
        )
    )
    pose.addParmTemplate(
        hou.ToggleParmTemplate(
            "ee_right_hand",
            "Right Hand",
            default_value=True,
            disable_when=pose_ee,
        )
    )
    pose.addParmTemplate(
        hou.ToggleParmTemplate(
            "ee_left_foot",
            "Left Foot",
            default_value=False,
            disable_when=pose_ee,
            join_with_next=True,
        )
    )
    pose.addParmTemplate(
        hou.ToggleParmTemplate(
            "ee_right_foot",
            "Right Foot",
            default_value=False,
            disable_when=pose_ee,
        )
    )
    con.addParmTemplate(pose)
    ptg.append(con)

    # Tab: Output - timing and the clip file.
    out = hou.FolderParmTemplate(
        "fld_output", "Output", folder_type=hou.folderType.Tabs
    )
    out.addParmTemplate(
        hou.IntParmTemplate(
            "start_frame",
            "Start Frame",
            1,
            script_callback="hou.pwd().type().hdaModule().refresh_starts(hou.pwd())",
            script_callback_language=hou.scriptLanguage.Python,
            default_expression=("$FSTART",),
            default_expression_language=(hou.scriptLanguage.Hscript,),
            min=-1000,
            max=1000,
            min_is_strict=False,
            max_is_strict=False,
            help="Scene frame on which the clip begins.\nThe __first__ sample holds before it, "
            "the __last__ sample holds after the clip ends.",
        )
    )
    out.addParmTemplate(
        hou.StringParmTemplate(
            "npz_path",
            "NPZ Path",
            1,
            default_value=(_NPZ_DEFAULT or "",),
            string_type=hou.stringParmType.FileReference,
            file_type=hou.fileType.Any,
            tags={"filechooser_pattern": "*.npz"},
            help="The clip the node reads. Set by Generate, or point it at any compatible Kimodo "
            "NPZ by hand (no server needed).",
        )
    )
    adv = hou.FolderParmTemplate(
        "grp_advanced",
        "Advanced",
        folder_type=hou.folderType.Collapsible,
        tags={"group_default": "0"},
    )
    adv.addParmTemplate(
        hou.ToggleParmTemplate(
            "retime",
            "Retime to Scene FPS",
            default_value=True,
            help="Map clip samples onto scene frames so the clip keeps its real duration at any "
            "`$FPS` (nearest sample, no blending).\n__Off__ = one clip sample "
            "per scene frame, so a 30 fps clip plays slow at 24 fps.",
        )
    )
    adv.addParmTemplate(
        hou.IntParmTemplate(
            "source_fps",
            "Clip FPS",
            1,
            default_value=(_KIMODO_FPS,),
            min=1,
            max=120,
            help="Frame rate Kimodo generated the clip at: `30` for the SOMA models.\n"
            "A property of the __model__, not of your scene. Do __not__ set it to "
            "`$FPS` or Retime becomes a no-op.",
        )
    )
    out.addParmTemplate(adv)
    ptg.append(out)

    # Tab: Server - set once.
    srv = hou.FolderParmTemplate(
        "fld_server", "Server", folder_type=hou.folderType.Tabs
    )
    srv.addParmTemplate(
        hou.StringParmTemplate(
            "server_url",
            "API Server URL",
            1,
            default_value=("http://localhost:8001",),
            join_with_next=True,
            help="URL of the running `kimodo_server`, e.g. "
            "`http://localhost:8001`.\nPoint at the GPU host if it runs elsewhere.",
        )
    )
    srv.addParmTemplate(
        hou.ButtonParmTemplate(
            "test_connection",
            "Test Connection",
            script_callback=_TEST_CB,
            script_callback_language=hou.scriptLanguage.Python,
            help="Ping the server's /health endpoint and report in Status.",
        )
    )
    srv.addParmTemplate(
        hou.StringParmTemplate(
            "download_dir",
            "Download Dir",
            1,
            default_value=("$HIP/kimodo_cache",),
            string_type=hou.stringParmType.FileReference,
            file_type=hou.fileType.Directory,
            help="Local folder where generated NPZ files are downloaded from the server.",
        )
    )
    ptg.append(srv)

    # Hidden plumbing.
    ptg.append(
        hou.StringParmTemplate(
            "job_id", "Job ID", 1, default_value=("",), is_hidden=True
        )
    )
    # Last failure message; the cook raises it as a node error. Cleared
    # when Generate starts.
    ptg.append(
        hou.StringParmTemplate(
            "last_error", "Last Error", 1, default_value=("",), is_hidden=True
        )
    )
    # 0..1 while a job runs, from the server's denoising progress. The
    # Timeline panel draws it.
    ptg.append(
        hou.FloatParmTemplate(
            "progress",
            "Progress",
            1,
            default_value=(0.0,),
            min=0.0,
            max=1.0,
            is_hidden=True,
        )
    )
    # Root path canonicalisation written by Generate: (origin x, origin z,
    # heading angle). The cook applies the inverse so the generated motion lands
    # on the authored curve.
    ptg.append(
        hou.FloatParmTemplate(
            "path_xform",
            "Path Transform",
            3,
            default_value=(0.0, 0.0, 0.0),
            is_hidden=True,
        )
    )
    ptg.append(
        hou.StringParmTemplate(
            "timeline_json",
            "Timeline",
            1,
            default_value=("",),
            is_hidden=True,
            tags={"editor": "1"},
        )
    )
    # Mirror of "timeline_json is non-empty" for disablewhen rules (a JSON blob
    # is not a value the conditional parser can compare against).
    ptg.append(
        hou.ToggleParmTemplate(
            "has_timeline", "Has Timeline", default_value=False, is_hidden=True
        )
    )
    ptg.append(
        hou.IntParmTemplate(
            "frame_ref",
            "Frame",
            1,
            default_expression=("$F",),
            default_expression_language=(hou.scriptLanguage.Hscript,),
            is_hidden=True,
        )
    )

    hda_def.setParmTemplateGroup(ptg)
    hda_def.save(str(hda_path))

    # Output connector labels live in the DialogScript as `outputlabel N "..."`
    # lines (right after the inputlabel block); inject them and re-save.
    ds = hda_def.sections()["DialogScript"].contents().splitlines(keepends=True)
    # name the (optional) input connectors
    _inlabels = {
        "1": "Root Path / Waypoints (opt)",
        "2": "Pose Keyframes / skeleton (opt)",
    }

    def _relabel(line):
        s = line.lstrip()
        for n, lbl in _inlabels.items():
            if s.startswith(("inputlabel\t%s" % n, "inputlabel %s" % n)):
                return '    inputlabel\t%s\t"%s"\n' % (n, lbl)
        return line

    ds = [_relabel(line) for line in ds]
    # A multiparm's count has no min/max in HOM; clamp it in the DialogScript so
    # the Segments block cannot be emptied to zero.
    for i, line in enumerate(ds):
        if line.strip() == 'name    "segments"':
            for j in range(i, min(i + 6, len(ds))):
                if ds[j].lstrip().startswith("default"):
                    indent = ds[j][: len(ds[j]) - len(ds[j].lstrip())]
                    ds.insert(j + 1, indent + "range   { 1! 100 }\n")
                    break
            break
    after = max(
        i for i, line in enumerate(ds) if line.lstrip().startswith("inputlabel")
    )
    inject = "".join(
        '    outputlabel\t%d\t"%s"\n' % (i + 1, lbl)
        for i, lbl in enumerate(labels)
    )
    hda_def.addSection(
        "DialogScript",
        "".join(ds[: after + 1]) + inject + "".join(ds[after + 1 :]),
    )
    hda_def.save(str(hda_path))

    print(
        f"HDA saved: {hda_path}  type: {hda_node.type().name()}  outputs: {labels}"
    )
    print(
        f"  parms: {[p.name() for p in hda_def.parmTemplateGroup().parmTemplates()]}"
    )


# At module scope on purpose: `hython scripts/create_hda.py` is the interface.
build_hda("kimodo_motion", "Kimodo Motion Generator", _HDA_PATH, _GENERATE_CB)
