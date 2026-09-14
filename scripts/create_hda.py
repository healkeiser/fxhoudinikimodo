"""
Create the vb::kimodo_motion HDA via hython (no GUI needed).

Usage:
    hython scripts/create_hda.py [npz_default]

Output: vb_kimodo_motion_1.1.hda in the repo root (packed; _add_help.py expands it into houdini/otls/).
"""
import os
import re
import sys
import hou

_HERE     = os.path.dirname(os.path.abspath(__file__))
_REPO     = os.path.dirname(_HERE)
# _HDA_PATH is defined below, after _LIBRARY.

# Embedded geometry built by scripts/build_skin.py (run it first). When present,
# the HDA gains the skin mesh (output 0) and the A-pose skeleton (output 1).
_SKIN_BGEO  = os.path.join(_REPO, "skin.bgeo.sc")
_APOSE_BGEO = os.path.join(_REPO, "apose.bgeo.sc")

# Kimodo SOMA models generate at this rate; exposed as the Source FPS parm default.
_KIMODO_FPS = 30

# Node icon (embedded into the HDA as its IconSVG section).
_ICON_SVG = os.path.join(_HERE, "kimodo_icon.svg")

# Node type is <namespace>::kimodo_motion::<version>; bump _VERSION for breaking UI changes.
_NAMESPACE = "vb"
_VERSION   = "1.1"
# Library file/dir name, derived from the type name the way Houdini does it.
_LIBRARY   = "%s_kimodo_motion_%s.hda" % (_NAMESPACE, _VERSION)
_HDA_PATH  = os.path.join(_REPO, _LIBRARY)


def _skin_sections():
    missing = [p for p in (_SKIN_BGEO, _APOSE_BGEO) if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(
            "Missing embedded geometry — run `hython scripts/build_skin.py` first:\n  "
            + "\n  ".join(missing)
        )
    return {
        "skin.bgeo.sc":  open(_SKIN_BGEO, "rb").read(),
        "apose.bgeo.sc": open(_APOSE_BGEO, "rb").read(),
    }

_NPZ_DEFAULT = sys.argv[1] if len(sys.argv) > 1 else ""

# The SOMA77 skeleton data (joints, parents, neutral pose, T-pose rotations) is the
# single source of truth in scripts/_soma77.py; it is embedded verbatim as the HDA's
# PythonModule section and read by each cook script via hou.pwd().parent().type().hdaModule().
_MODULE_SRC = open(os.path.join(_HERE, "_soma77.py"), encoding="utf-8").read()

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

    npz_path = hda_node.parm("npz_path").eval()
    if not npz_path:
        return  # no NPZ yet — output empty geometry, wait for Generate

    geo = node.geometry()
    data        = np.load(npz_path)
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

    pos  = posed[frame]
    grot = global_rots[frame]

    # Optional: Kimodo's per-frame foot-contact labels. Absent from NPZs that only
    # carry the two keys above, so the attribute is written only when they exist.
    contacts = {}
    if "foot_contacts" in data.files:
        fc = data["foot_contacts"]
        channels = _FOOT_CHANNELS.get(fc.shape[-1])
        if channels:
            contacts = {n: int(v) for n, v in zip(channels, fc[frame])}

    paths = []
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
import json, threading, time, requests, hou
import numpy as np

node         = kwargs["node"]
url          = node.parm("server_url").eval().rstrip("/")
download_dir = node.parm("download_dir").eval()
fps          = hou.fps()
source_fps   = node.parm("source_fps").eval() or 30
start_frame  = node.parm("start_frame").eval()
retime       = bool(node.parm("retime").eval())

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
    if ins and ins[0] is not None:
        geo = node.inputGeometry(0)
        pts = geo.points()
        if pts:
            xz = [[float(p.position()[0]), float(p.position()[2])] for p in pts]
            if geo.findPointAttrib("frame") is not None:
                frames = [int(p.attribValue("frame")) for p in pts]
            elif len(pts) > 1:
                # num_frames = int(duration * source_fps)  (clip samples, not scene frames)
                T = max(2, int(duration_s * node.parm("source_fps").eval()))
                frames = [int(round(i * (T - 1) / (len(pts) - 1))) for i in range(len(pts))]
            else:
                frames = [0]
            # dedup by frame (keep first), sort by frame
            by_frame = {}
            for f, c in zip(frames, xz):
                by_frame.setdefault(f, c)
            items = sorted(by_frame.items())
            root2d = {"type": "root2d",
                      "frame_indices": [f for f, _ in items],
                      "smooth_root_2d": [c for _, c in items]}
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
    node.parm("status").set(f"Error: {e}")
    if hou.isUIAvailable():
        hou.ui.displayMessage(str(e), severity=hou.severityType.Error, title="Kimodo")
    else:
        print("Kimodo:", e)
else:
    job_id = resp.json()["job_id"]
    node.parm("job_id").set(job_id)
    node.parm("status").set(f"Queued ({job_id[:8]}...)")
    if hou.isUIAvailable():
        hou.ui.setStatusMessage("Kimodo: generation started, watch the node's Status field.",
                                severity=hou.severityType.ImportantMessage)

    def _poll():
        # HOM/UI calls are not thread-safe: marshal them to the main thread.
        import hdefereval, os
        def _set(parm, val):
            hdefereval.executeInMainThreadWithResult(lambda: node.parm(parm).set(val))
        def _msg(text, severity=None):
            if not hou.isUIAvailable():
                print("Kimodo:", text); return
            if severity is None:
                hdefereval.executeDeferred(lambda: hou.ui.displayMessage(text, title="Kimodo"))
            else:
                hdefereval.executeDeferred(lambda: hou.ui.displayMessage(text, severity=severity, title="Kimodo"))
        def _statusbar(text, severity=hou.severityType.ImportantMessage):
            if hou.isUIAvailable():
                hdefereval.executeDeferred(lambda: hou.ui.setStatusMessage(text, severity=severity))
        fails = 0
        while True:
            time.sleep(5)
            # stop if a newer Generate has replaced this job
            if hdefereval.executeInMainThreadWithResult(lambda: node.parm("job_id").eval()) != job_id:
                break
            try:
                r = requests.get(f"{url}/jobs/{job_id}", timeout=10)
                if r.status_code == 404:
                    _set("status", "Job lost (server restarted?)")
                    _set("job_id", "")
                    break
                r.raise_for_status()
                data = r.json()
                fails = 0
            except Exception as e:
                fails += 1
                _set("status", f"Poll error ({fails}/3): {e}")
                if fails >= 3:
                    break
                continue
            status  = data["status"]
            elapsed = data.get("elapsed")
            elapsed_str = f" ({int(elapsed)}s)" if elapsed else ""
            if status == "done":
                frames, joints = data["frames"], data["joints"]
                done_label = f"Done{elapsed_str}" + (" (cached)" if data.get("cached") else "")
                try:
                    _set("status", f"Downloading...{elapsed_str}")
                    os.makedirs(download_dir, exist_ok=True)
                    # forward slashes: Houdini's own convention, and what the user typed
                    local_npz = download_dir.rstrip("/\\") + f"/{job_id}.npz"
                    with requests.get(f"{url}/jobs/{job_id}/download", timeout=120, stream=True) as dl:
                        dl.raise_for_status()
                        with open(local_npz, "wb") as fh:
                            for chunk in dl.iter_content(chunk_size=1 << 20):
                                fh.write(chunk)
                except Exception as e:
                    _set("status", f"Download failed: {e}")
                    _msg(f"NPZ download failed:\n{e}", severity=hou.severityType.Error)
                    break
                def _finish():
                    fps = node.parm("source_fps").eval() or 30
                    secs = frames / fps
                    node.parm("status").set(done_label)
                    node.parm("clip_info").set(
                        f"{secs:.2f} s = {round(secs * hou.fps())} frames @ {hou.fps():g} fps "
                        f"({frames} samples @ {fps} fps)")
                    node.parm("npz_path").set(local_npz)
                    node.parm("job_id").set("")
                    node.cook(force=True)
                hdefereval.executeInMainThreadWithResult(_finish)
                _statusbar(f"Kimodo: generated {frames} frames ({joints} joints){elapsed_str}.")
                break
            elif status in ("failed", "cancelled"):
                err = data.get("error")
                _set("status", f"{status.capitalize()}: {err[:60]}" if err else status.capitalize())
                if status == "failed":
                    _msg(f"Generation failed:\n{err}", severity=hou.severityType.Error)
                break
            else:
                _set("status", f"Running...{elapsed_str}")

    threading.Thread(target=_poll, daemon=True).start()
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
        hou.ui.displayMessage(str(e), severity=hou.severityType.Error, title="Kimodo")
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

# Build a standalone A-pose rig to pose for full-body / end-effector constraints.
# It loads the HDA's embedded apose section by *type name*, so it's independent of this
# node's outputs — no output-into-its-own-input loop. Wired into input 1 ready to pose.
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
hou.ui.displayMessage(
    "Created an independent A-pose rig%s.\n\nPose / keyframe it, set Pose Keyframes, choose "
    "Full-Body or End-Effector, then Generate.%s" % (
        " + Rig Pose" if tip is not rig else "",
        "" if wired else "\n\nWire it into this node's input 1.",
    ),
    title="Kimodo",
)
"""

_REST_SCRIPT = r"""
import hou

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
# the HDA definition, writes them to a temp file, and loads them — avoids opdef:
# path resolution and keeps the HDA self-contained.
_SECTION_LOADER = '''import os, base64, tempfile, hou
node = hou.pwd()
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
'''

# Open Timeline: focus an existing Kimodo Timeline pane tab or float a new one, and select
# this node so the panel picks it up.
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
        hou.ui.displayMessage("Kimodo Timeline panel not found.\nIs houdini/python_panels on your HOUDINI_PATH (fxhoudinikimodo package)?",
                              severity=hou.severityType.Error, title="Kimodo")
    else:
        tab = desk.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 420))
        tab.setActiveInterface(iface)
if tab is not None:
    tab.setIsCurrentTab()
"""

# Runs when a node of this type is created: colour + shape so it reads as a generator.
_ON_CREATED = r"""
node = kwargs["node"]
node.setColor(hou.Color((0.46, 0.73, 0.35)))
node.setUserData("nodeshape", "bulge")
"""

_PROMPT_HELP = (
    "What the character does, in plain <b>English</b>. Be specific about body part, "
    "direction, speed and style.<br><br>"
    "<b>Examples</b><br>"
    "<i>a person walks forward slowly</i><br>"
    "<i>someone jogs in a circle then stops</i><br>"
    "<i>a person waves with the right hand, then bows</i><br>"
    "<i>a tired person sits down on a chair</i>"
)


def build_hda(node_name, description, hda_path, generate_cb, skin_sections=None):
    """Build the kimodo_motion HDA.

    Output order follows SideFX's character/test-geometry nodes so a Joint Deform wires
    straight across (0 -> 0, 1 -> 1, 2 -> 2):
      0 Rest Geometry (skin mesh)   1 Capture Pose (A-pose)   2 Animated Pose   3 T-Pose
    skin_sections: {section_name: bytes} from build_skin.py. When None, only
    Animated Pose (0) and T-Pose (1) are produced.
    """
    obj = hou.node("/obj")
    geo = obj.createNode("geo", node_name + "_setup")
    geo.deleteItems(geo.children())

    subnet = geo.createNode("subnet", node_name + "_subnet")

    # Both cook scripts read the SOMA77 data from the HDA's PythonModule section
    # (added below) via hou.pwd().parent().type().hdaModule() — single source.
    anim_sop = subnet.createNode("python", "animated_sop")
    anim_sop.parm("python").set(_COOK_SCRIPT)
    tpose_sop = subnet.createNode("python", "tpose_sop")
    tpose_sop.parm("python").set(_REST_SCRIPT)

    def _out(idx, src):
        o = subnet.createNode("output", "output%d" % idx)
        o.setInput(0, src)
        o.parm("outputidx").set(idx)
        return o

    if skin_sections:
        skin_sop = subnet.createNode("python", "skin_sop")
        skin_sop.parm("python").set(_SECTION_LOADER % "skin.bgeo.sc")
        apose_sop = subnet.createNode("python", "apose_sop")
        apose_sop.parm("python").set(_SECTION_LOADER % "apose.bgeo.sc")
        first = _out(0, skin_sop)
        _out(1, apose_sop)
        _out(2, anim_sop)
        _out(3, tpose_sop)
        labels = ["Rest Geometry", "Capture Pose", "Animated Pose", "T-Pose"]
    else:
        first = _out(0, anim_sop)
        _out(1, tpose_sop)
        labels = ["Animated Pose", "T-Pose"]

    first.setDisplayFlag(True)
    first.setRenderFlag(True)
    subnet.layoutChildren()

    # createDigitalAsset appends to an existing library file; start clean so the packed
    # file holds exactly one definition (the one _add_help.py picks up).
    if os.path.exists(hda_path):
        os.remove(hda_path)
    hda_node = subnet.createDigitalAsset(
        name="%s::%s::%s" % (_NAMESPACE, node_name, _VERSION),   # version must be in the name
        hda_file_name=hda_path,
        description=description,
        min_num_inputs=0,
        max_num_inputs=2,   # input 0: geometry -> root2d; input 1: posed skeleton -> fullbody/EE
        version=_VERSION,
    )
    hda_def = hda_node.type().definition()
    hda_def.setMaxNumOutputs(len(labels))
    # Node icon: the SVG in scripts/kimodo_icon.svg, embedded as the IconSVG section.
    # Namespaced opdef form: opdef:/<namespace>::Sop/<name>::<version>?IconSVG
    scope, ns, base, ver = hda_node.type().nameComponents()
    icon_path = "opdef:/%sSop/%s%s?IconSVG" % (ns + "::" if ns else "", base, "::" + ver if ver else "")
    hda_def.addSection("IconSVG", open(_ICON_SVG, encoding="utf-8").read())
    hda_def.setIcon(icon_path)
    hda_def.addSection("PythonModule", _MODULE_SRC)   # SOMA77 data for the cook scripts
    hda_def.addSection("OnCreated", _ON_CREATED)
    hda_def.setExtraFileOption("OnCreated/IsPython", True)
    # Shown under the node in the network editor (Type Properties > Node > Descriptive Parm).
    hda_def.addSection("DescriptiveParmName", "status")
    if skin_sections:
        # Store the binary bgeo as base64 text so the section round-trips cleanly
        # (HDASection.contents() returns str; raw bytes don't survive that).
        import base64
        for sname, data in skin_sections.items():
            hda_def.addSection(sname, base64.b64encode(data).decode("ascii"))

    # Tab menu: Kimodo instead of the generic "Digital Assets" submenu.
    shelf = hda_def.sections().get("Tools.shelf")
    if shelf is not None:
        xml = re.sub(r"<toolSubmenu>.*?</toolSubmenu>", "<toolSubmenu>Kimodo</toolSubmenu>",
                     shelf.contents(), count=1)
        hda_def.addSection("Tools.shelf", xml)

    # ── parameter interface ──────────────────────────────────────────────────
    ptg = hou.ParmTemplateGroup()   # start fresh — no inherited subnet parms
    always_off = '{ status != "__never__" }'   # disablewhen that is always true: read-only field
    timeline_owns = '{ has_timeline == 1 }'   # the Timeline panel drives these while it has data

    # Tab: Generate — the everyday controls.
    gen = hou.FolderParmTemplate("fld_generate", "Generate", folder_type=hou.folderType.Tabs)
    gen.addParmTemplate(hou.StringParmTemplate(
        "prompt", "Prompt", 1,
        default_value=("a person walks forward",),
        tags={"editor": "1", "editorlines": "4-8"},
        help=_PROMPT_HELP,
    ))
    gen.addParmTemplate(hou.ButtonParmTemplate(
        "open_timeline", "Open Timeline",
        script_callback=_OPEN_TIMELINE_CB,
        script_callback_language=hou.scriptLanguage.Python,
        help="Open the <b>Kimodo Timeline</b> panel for this node: prompt segments laid end to "
             "end, transitions, and Full Body / hand / foot pose tracks.<br>While a timeline "
             "exists it owns Duration and the pose parameters below.",
    ))
    gen.addParmTemplate(hou.IntParmTemplate(
        "duration_frames", "Duration (frames)", 1,
        default_value=(72,),
        min=12, max=720, min_is_strict=False, max_is_strict=False,
        disable_when=timeline_owns,
        help="Length of the clip in <b>scene frames</b> at the current <code>$FPS</code> "
             "(<code>72</code> = 3 s at 24 fps).<br>Converted to seconds for Kimodo, which "
             "generates at 30 fps; with <b>Retime to Scene FPS</b> on you get back exactly this "
             "many frames.",
    ))
    gen.addParmTemplate(hou.MenuParmTemplate(
        "model", "Model",
        ("Kimodo-SOMA-RP-v1.1", "Kimodo-SOMA-SEED-v1.1", "Kimodo-SOMA-RP-v1"),
        default_value=0,
        help="Kimodo checkpoint.<br><b>RP</b> conditions on a rest pose.<br><b>SEED</b> uses a "
             "fixed seed for reproducible results.",
    ))
    gen.addParmTemplate(hou.ButtonParmTemplate(
        "generate", "Generate",
        script_callback=generate_cb,
        script_callback_language=hou.scriptLanguage.Python,
        join_with_next=True,
        help="Send the prompt to the server. Runs in the background; the node recooks when "
             "the clip has downloaded.<br>Progress shows in <b>Status</b> and under the node.",
    ))
    gen.addParmTemplate(hou.ButtonParmTemplate(
        "cancel", "Cancel",
        script_callback=_CANCEL_CB,
        script_callback_language=hou.scriptLanguage.Python,
        join_with_next=True,
        help="Cancel the queued job or discard the running one.",
    ))
    gen.addParmTemplate(hou.ToggleParmTemplate(
        "force", "Force Regenerate",
        default_value=False,
        help="Bypass the server cache and run inference again even if an identical "
             "<i>prompt + duration + model + constraints</i> was generated before.",
    ))
    gen.addParmTemplate(hou.StringParmTemplate(
        "status", "Status", 1,
        default_value=("",),
        disable_when=always_off,
        help="Live job state: <code>Queued</code>, <code>Running (Ns)</code>, "
             "<code>Downloading</code>, <code>Done (Ns)</code>, <code>Done (cached)</code>, "
             "<code>Failed</code>, <code>Cancelled</code>.<br>Also shows the "
             "<b>Test Connection</b> result.",
    ))
    gen.addParmTemplate(hou.StringParmTemplate(
        "clip_info", "Clip", 1,
        default_value=("",),
        disable_when=always_off,
        help="Length of the last generated clip in samples and seconds.",
    ))
    ptg.append(gen)

    # Tab: Constraints — optional steering.
    con = hou.FolderParmTemplate("fld_constraints", "Constraints", folder_type=hou.folderType.Tabs)
    # Collapsible groups instead of separators; group_default 1 = open, 0 = closed on creation.
    js = hou.FolderParmTemplate("grp_json", "Constraints JSON", folder_type=hou.folderType.Collapsible,
                                tags={"group_default": "0"})
    js.addParmTemplate(hou.StringParmTemplate(
        "constraints_file", "Constraints File", 1,
        default_value=("",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.json"},
        help="Optional Kimodo constraints JSON (e.g. exported from the Kimodo demo). "
             "Ignored when Constraints JSON below is non-empty.",
    ))
    js.addParmTemplate(hou.StringParmTemplate(
        "constraints_json", "Constraints JSON", 1,
        default_value=("",),
        tags={"editor": "1", "editorlines": "3-8"},
        help="Optional inline Kimodo constraints JSON (a list of constraint dicts). "
             "Takes precedence over <b>Constraints File</b>.<br><br><b>Example root path</b><br>"
             '<code>[{"type": "root2d", "frame_indices": [0, 90], '
             '"smooth_root_2d": [[0,0],[2,1]]}]</code>',
    ))
    con.addParmTemplate(js)
    pose = hou.FolderParmTemplate("grp_pose", "Pose Keyframes (input 1)", folder_type=hou.folderType.Collapsible,
                                  tags={"group_default": "1"})
    pose.addParmTemplate(hou.ButtonParmTemplate(
        "make_pose_rig", "Create Pose Rig",
        script_callback=_MAKE_RIG_CB,
        script_callback_language=hou.scriptLanguage.Python,
        help="Drop an independent A-pose rig (+ Rig Pose) into the network and wire it to "
             "input 1. Pose / keyframe it to author full-body / end-effector constraints.",
    ))
    pose.addParmTemplate(hou.StringParmTemplate(
        "pose_keyframes", "Pose Keyframes", 1,
        default_value=("",),
        disable_when=timeline_owns,
        help="Frame numbers to sample the input-1 skeleton at, e.g. <code>0 45 89</code>.<br>"
             "Empty = no pose constraint. Disabled while the Timeline panel owns the keys.",
    ))
    pose.addParmTemplate(hou.MenuParmTemplate(
        "pose_type", "Pose Constraint",
        ("Full-Body", "End-Effector"),
        default_value=0,
        disable_when='{ pose_keyframes == "" } ' + timeline_owns,
        help="How to use the posed skeleton on input 1: constrain the whole body, "
             "or only the selected hands/feet.",
    ))
    pose_ee = '{ pose_type != "End-Effector" } { pose_keyframes == "" } ' + timeline_owns
    pose.addParmTemplate(hou.ToggleParmTemplate("ee_left_hand", "Left Hand", default_value=False,
                                                disable_when=pose_ee, join_with_next=True))
    pose.addParmTemplate(hou.ToggleParmTemplate("ee_right_hand", "Right Hand", default_value=True,
                                                disable_when=pose_ee))
    pose.addParmTemplate(hou.ToggleParmTemplate("ee_left_foot", "Left Foot", default_value=False,
                                                disable_when=pose_ee, join_with_next=True))
    pose.addParmTemplate(hou.ToggleParmTemplate("ee_right_foot", "Right Foot", default_value=False,
                                                disable_when=pose_ee))
    con.addParmTemplate(pose)
    ptg.append(con)

    # Tab: Output — timing and the clip file.
    out = hou.FolderParmTemplate("fld_output", "Output", folder_type=hou.folderType.Tabs)
    out.addParmTemplate(hou.IntParmTemplate(
        "start_frame", "Start Frame", 1,
        default_expression=("$FSTART",),
        default_expression_language=(hou.scriptLanguage.Hscript,),
        min=-1000, max=1000, min_is_strict=False, max_is_strict=False,
        help="Scene frame on which the clip begins.<br>The <b>first</b> sample holds before it, "
             "the <b>last</b> sample holds after the clip ends.",
    ))
    out.addParmTemplate(hou.StringParmTemplate(
        "npz_path", "NPZ Path", 1,
        default_value=(_NPZ_DEFAULT or "",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.npz"},
        help="The clip the node reads. Set by Generate, or point it at any compatible Kimodo "
             "NPZ by hand (no server needed).",
    ))
    adv = hou.FolderParmTemplate("grp_advanced", "Advanced", folder_type=hou.folderType.Collapsible,
                                 tags={"group_default": "0"})
    adv.addParmTemplate(hou.ToggleParmTemplate(
        "retime", "Retime to Scene FPS",
        default_value=True,
        help="Map clip samples onto scene frames so the clip keeps its real duration at any "
             "<code>$FPS</code> (nearest sample, no blending).<br><b>Off</b> = one clip sample "
             "per scene frame, so a 30 fps clip plays slow at 24 fps.",
    ))
    adv.addParmTemplate(hou.IntParmTemplate(
        "source_fps", "Clip FPS", 1,
        default_value=(_KIMODO_FPS,), min=1, max=120,
        help="Frame rate Kimodo generated the clip at: <code>30</code> for the SOMA models.<br>"
             "A property of the <b>model</b>, not of your scene. Do <b>not</b> set it to "
             "<code>$FPS</code> or Retime becomes a no-op.",
    ))
    out.addParmTemplate(adv)
    ptg.append(out)

    # Tab: Server — set once.
    srv = hou.FolderParmTemplate("fld_server", "Server", folder_type=hou.folderType.Tabs)
    srv.addParmTemplate(hou.StringParmTemplate(
        "server_url", "API Server URL", 1,
        default_value=("http://localhost:8001",),
        join_with_next=True,
        help="URL of the running <code>kimodo_server</code>, e.g. "
             "<code>http://localhost:8001</code>.<br>Point at the GPU host if it runs elsewhere.",
    ))
    srv.addParmTemplate(hou.ButtonParmTemplate(
        "test_connection", "Test Connection",
        script_callback=_TEST_CB,
        script_callback_language=hou.scriptLanguage.Python,
        help="Ping the server's /health endpoint and report in Status.",
    ))
    srv.addParmTemplate(hou.StringParmTemplate(
        "download_dir", "Download Dir", 1,
        default_value=("$HIP/kimodo_cache",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Directory,
        help="Local folder where generated NPZ files are downloaded from the server.",
    ))
    ptg.append(srv)

    # Hidden plumbing.
    ptg.append(hou.StringParmTemplate("job_id", "Job ID", 1, default_value=("",), is_hidden=True))
    ptg.append(hou.StringParmTemplate("timeline_json", "Timeline", 1, default_value=("",), is_hidden=True,
                                      tags={"editor": "1"}))
    # Mirror of "timeline_json is non-empty" for disablewhen rules (a JSON blob is not a
    # value the conditional parser can compare against).
    ptg.append(hou.ToggleParmTemplate("has_timeline", "Has Timeline", default_value=False, is_hidden=True))
    ptg.append(hou.IntParmTemplate(
        "frame_ref", "Frame", 1,
        default_expression=("$F",),
        default_expression_language=(hou.scriptLanguage.Hscript,),
        is_hidden=True,
    ))

    hda_def.setParmTemplateGroup(ptg)
    hda_def.save(hda_path)

    # Output connector labels live in the DialogScript as `outputlabel N "..."`
    # lines (right after the inputlabel block); inject them and re-save.
    ds = hda_def.sections()["DialogScript"].contents().splitlines(keepends=True)
    # name the (optional) input connectors
    _inlabels = {"1": "Root Path / Waypoints (opt)", "2": "Pose Keyframes / skeleton (opt)"}
    def _relabel(line):
        s = line.lstrip()
        for n, lbl in _inlabels.items():
            if s.startswith(("inputlabel\t%s" % n, "inputlabel %s" % n)):
                return '    inputlabel\t%s\t"%s"\n' % (n, lbl)
        return line
    ds = [_relabel(line) for line in ds]
    after = max(i for i, line in enumerate(ds) if line.lstrip().startswith("inputlabel"))
    inject = "".join('    outputlabel\t%d\t"%s"\n' % (i + 1, lbl) for i, lbl in enumerate(labels))
    hda_def.addSection("DialogScript", "".join(ds[:after + 1]) + inject + "".join(ds[after + 1:]))
    hda_def.save(hda_path)

    print(f"HDA saved: {hda_path}  type: {hda_node.type().name()}  outputs: {labels}")
    print(f"  parms: {[p.name() for p in hda_def.parmTemplateGroup().parmTemplates()]}")


# ── build the HDA ────────────────────────────────────────────────────────────
build_hda("kimodo_motion", "Kimodo Motion Generator",
          _HDA_PATH, _GENERATE_CB, skin_sections=_skin_sections())
