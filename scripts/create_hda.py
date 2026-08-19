"""
Create kimodo_motion.hda via hython (no GUI needed).

Usage:
    hython scripts/create_hda.py [npz_default]

Output: kimodo_motion.hda in the repo root.
"""
import os
import sys
import hou

_HERE     = os.path.dirname(os.path.abspath(__file__))
_REPO     = os.path.dirname(_HERE)
_HDA_PATH = os.path.join(_REPO, "kimodo_motion.hda")

# Embedded geometry built by scripts/build_skin.py (run it first). When present,
# the HDA gains the A-pose skeleton (output1) and skin mesh (output2).
_SKIN_BGEO  = os.path.join(_REPO, "skin.bgeo.sc")
_APOSE_BGEO = os.path.join(_REPO, "apose.bgeo.sc")


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

    frame = hda_node.parm("frame_ref").eval() - 1
    frame = max(0, min(frame, T - 1))

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
    # gives a bone-aligned world orientation matching output1. Houdini KineFX is
    # row-vector / row-major, so we transpose; translation goes in the last row.
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

try:
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
                # SOMA models run at 30 fps: num_frames = int(duration * 30)
                T = max(2, int(node.parm("duration").eval() * 30))
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
    kf = node.parm("pose_keyframes").eval().strip()
    if len(ins) > 1 and ins[1] is not None and kf:
        frames = [int(x) for x in kf.replace(",", " ").split()]
        m = node.type().hdaModule()
        joints = list(m.SOMA77_JOINTS)
        tp = np.asarray(m.TPOSE_ROTS, dtype=float).reshape(-1, 3, 3)
        idx = {n: i for i, n in enumerate(joints)}
        src = ins[1]
        pos_kf, rot_kf, sr_kf = [], [], []
        for f in frames:
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
        is_ee = node.parm("pose_type").evalAsString() == "End-Effector"
        cdict = {"frame_indices": frames,
                 "global_joints_positions": pos_kf,
                 "global_joints_rots": rot_kf,
                 "smooth_root_2d": sr_kf}
        if is_ee:
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

    resp = requests.post(
        f"{url}/generate",
        json={
            "prompt":      node.parm("prompt").eval(),
            "duration":    node.parm("duration").eval(),
            "model":       node.parm("model").evalAsString(),
            "force":       bool(node.parm("force").eval()),
            "constraints": constraints,
        },
        timeout=30,
    )
    resp.raise_for_status()
except Exception as e:
    hou.ui.displayMessage(str(e), severity=hou.severityType.Error, title="Kimodo")
    node.parm("status").set(f"Error: {e}")
else:
    job_id = resp.json()["job_id"]
    node.parm("job_id").set(job_id)
    node.parm("status").set(f"Queued ({job_id[:8]}...)")

    def _poll():
        # HOM/UI calls are not thread-safe: marshal them to the main thread.
        import hdefereval, os
        def _set(parm, val):
            hdefereval.executeInMainThreadWithResult(lambda: node.parm(parm).set(val))
        def _msg(text, severity=None):
            if severity is None:
                hdefereval.executeDeferred(lambda: hou.ui.displayMessage(text, title="Kimodo"))
            else:
                hdefereval.executeDeferred(lambda: hou.ui.displayMessage(text, severity=severity, title="Kimodo"))
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
                    local_npz = os.path.join(download_dir, f"{job_id}.npz")
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
                    node.parm("status").set(done_label)
                    node.parm("npz_path").set(local_npz)
                    node.parm("job_id").set("")
                    node.cook(force=True)
                hdefereval.executeInMainThreadWithResult(_finish)
                _msg(f"Generated {frames} frames ({joints} joints){elapsed_str}.")
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
    hou.ui.displayMessage(
        "Generation started in background.\nHoudini will update automatically when done.",
        title="Kimodo",
    )
"""

# Cancel is identical for both HDAs (same server endpoint).
_CANCEL_CB = r"""
import requests, hou

node   = kwargs["node"]
url    = node.parm("server_url").eval().rstrip("/")
job_id = node.parm("job_id").eval()
if not job_id:
    hou.ui.displayMessage("No active job to cancel.", title="Kimodo")
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


def build_hda(node_name, description, hda_path, generate_cb, skin_sections=None):
    """Build the kimodo_motion HDA.

    skin_sections: optional {section_name: bytes}. When given, the HDA gets four
      outputs (0 animated, 1 A-pose rest skeleton, 2 skin mesh, 3 T-pose) with the
      skeleton/mesh geometry embedded as sections. When None, two outputs
      (0 animated, 1 T-pose).
    """
    obj = hou.node("/obj")
    geo = obj.createNode("geo", node_name + "_setup")
    geo.deleteItems(geo.children())

    subnet = geo.createNode("subnet", node_name + "_subnet")

    # Both cook scripts read the SOMA77 data from the HDA's PythonModule section
    # (added below) via hou.pwd().parent().type().hdaModule() — single source.
    inner = subnet.createNode("python", "gen_sop")
    inner.parm("python").set(_COOK_SCRIPT)
    rest_sop = subnet.createNode("python", "rest_sop")
    rest_sop.parm("python").set(_REST_SCRIPT)

    out0 = subnet.createNode("output", "output0")
    out0.setInput(0, inner)
    out0.parm("outputidx").set(0)

    if skin_sections:
        apose_sop = subnet.createNode("python", "apose_sop")
        apose_sop.parm("python").set(_SECTION_LOADER % "apose.bgeo.sc")
        skin_sop = subnet.createNode("python", "skin_sop")
        skin_sop.parm("python").set(_SECTION_LOADER % "skin.bgeo.sc")
        out1 = subnet.createNode("output", "output1")
        out1.setInput(0, apose_sop)
        out1.parm("outputidx").set(1)
        out2 = subnet.createNode("output", "output2")
        out2.setInput(0, skin_sop)
        out2.parm("outputidx").set(2)
        out3 = subnet.createNode("output", "output3")
        out3.setInput(0, rest_sop)
        out3.parm("outputidx").set(3)
        n_outputs = 4
    else:
        out1 = subnet.createNode("output", "output1")
        out1.setInput(0, rest_sop)
        out1.parm("outputidx").set(1)
        n_outputs = 2

    out0.setDisplayFlag(True)
    out0.setRenderFlag(True)
    subnet.layoutChildren()

    hda_node = subnet.createDigitalAsset(
        name=node_name,
        hda_file_name=hda_path,
        description=description,
        min_num_inputs=0,
        max_num_inputs=2,   # input 0: geometry -> root2d; input 1: posed skeleton -> fullbody/EE
        version="1.0",
    )
    hda_def = hda_node.type().definition()
    hda_def.setMaxNumOutputs(n_outputs)
    hda_def.addSection("PythonModule", _MODULE_SRC)   # SOMA77 data for the cook scripts
    if skin_sections:
        # Store the binary bgeo as base64 text so the section round-trips cleanly
        # (HDASection.contents() returns str; raw bytes don't survive that).
        import base64
        for sname, data in skin_sections.items():
            hda_def.addSection(sname, base64.b64encode(data).decode("ascii"))
    ptg = hou.ParmTemplateGroup()   # start fresh — no inherited subnet parms

    ptg.append(hou.SeparatorParmTemplate("sep_api"))
    ptg.append(hou.StringParmTemplate(
        "server_url", "API Server URL", 1,
        default_value=("http://localhost:8001",),
    ))
    ptg.append(hou.StringParmTemplate(
        "download_dir", "Download Dir", 1,
        default_value=("$HIP/kimodo_cache",),
        help="Local folder where generated NPZ files are downloaded from the server.",
    ))
    ptg.append(hou.SeparatorParmTemplate("sep_gen"))
    ptg.append(hou.StringParmTemplate(
        "prompt", "Prompt", 1,
        default_value=("a person walks forward",),
    ))
    ptg.append(hou.FloatParmTemplate(
        "duration", "Duration (s)", 1,
        default_value=(3.0,), min=0.5, max=30.0,
    ))
    ptg.append(hou.MenuParmTemplate(
        "model", "Model",
        ("Kimodo-SOMA-RP-v1.1", "Kimodo-SOMA-SEED-v1.1", "Kimodo-SOMA-RP-v1"),
        default_value=0,
    ))
    ptg.append(hou.ToggleParmTemplate(
        "force", "Force Regenerate",
        default_value=False,
        help="Bypass the server cache and re-run inference even if a matching clip exists.",
    ))
    ptg.append(hou.SeparatorParmTemplate("sep_constraints"))
    ptg.append(hou.StringParmTemplate(
        "constraints_file", "Constraints File", 1,
        default_value=("",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.json"},
        help="Optional Kimodo constraints JSON (e.g. exported from the Kimodo demo). "
             "Ignored when Constraints JSON below is non-empty.",
    ))
    ptg.append(hou.StringParmTemplate(
        "constraints_json", "Constraints JSON", 1,
        default_value=("",),
        tags={"editor": "1"},
        help="Optional inline Kimodo constraints JSON (a list of constraint dicts). "
             "Takes precedence over Constraints File.",
    ))
    ptg.append(hou.SeparatorParmTemplate("sep_pose"))
    ptg.append(hou.MenuParmTemplate(
        "pose_type", "Pose Constraint",
        ("Full-Body", "End-Effector"),
        default_value=0,
        help="How to use a posed skeleton wired to input 1: constrain the whole body, "
             "or only selected end-effectors (hands/feet).",
    ))
    pose_ee = {"disablewhen": '{ pose_type != "End-Effector" }'}
    ptg.append(hou.ToggleParmTemplate("ee_left_hand", "Left Hand", default_value=False, tags=pose_ee))
    ptg.append(hou.ToggleParmTemplate("ee_right_hand", "Right Hand", default_value=True, tags=pose_ee))
    ptg.append(hou.ToggleParmTemplate("ee_left_foot", "Left Foot", default_value=False, tags=pose_ee))
    ptg.append(hou.ToggleParmTemplate("ee_right_foot", "Right Foot", default_value=False, tags=pose_ee))
    ptg.append(hou.StringParmTemplate(
        "pose_keyframes", "Pose Keyframes", 1,
        default_value=("",),
        help="Frame numbers to sample the input-1 skeleton at, e.g. `0 45 89`. Empty = no "
             "pose constraint.",
    ))
    ptg.append(hou.ButtonParmTemplate(
        "make_pose_rig", "Create Pose Rig",
        script_callback=_MAKE_RIG_CB,
        script_callback_language=hou.scriptLanguage.Python,
        help="Drop an independent A-pose rig (+ Rig Pose) into the network and wire it to "
             "input 1. Pose / keyframe it to author full-body / end-effector constraints.",
    ))
    ptg.append(hou.ButtonParmTemplate(
        "generate", "Generate",
        script_callback=generate_cb,
        script_callback_language=hou.scriptLanguage.Python,
        join_with_next=True,
    ))
    ptg.append(hou.ButtonParmTemplate(
        "cancel", "Cancel",
        script_callback=_CANCEL_CB,
        script_callback_language=hou.scriptLanguage.Python,
    ))
    ptg.append(hou.StringParmTemplate(
        "status", "Status", 1,
        default_value=("",),
        help="Current job status. Updated automatically by Generate.",
    ))
    ptg.append(hou.StringParmTemplate(
        "job_id", "Job ID", 1,
        default_value=("",),
        is_hidden=True,
    ))
    ptg.append(hou.IntParmTemplate(
        "frame_ref", "Frame", 1,
        default_expression=("$F",),
        default_expression_language=(hou.scriptLanguage.Hscript,),
        is_hidden=True,
    ))
    ptg.append(hou.SeparatorParmTemplate("sep_npz"))
    ptg.append(hou.StringParmTemplate(
        "npz_path", "NPZ Path", 1,
        default_value=(_NPZ_DEFAULT or "",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.npz"},
    ))

    hda_def.setParmTemplateGroup(ptg)
    hda_def.save(hda_path)

    # Output connector labels live in the DialogScript as `outputlabel N "..."`
    # lines (right after the inputlabel block); inject them and re-save.
    labels = (["Animated Pose", "Capture Pose", "Rest Geometry", "T-Pose"]
              if skin_sections else ["Animated Pose", "T-Pose"])
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

    print(f"HDA saved: {hda_path}  outputs: {labels}")
    print(f"  parms: {[p.name() for p in hda_def.parmTemplateGroup().parmTemplates()]}")


# ── build the HDA ────────────────────────────────────────────────────────────
build_hda("kimodo_motion", "Kimodo Motion Generator",
          _HDA_PATH, _GENERATE_CB, skin_sections=_skin_sections())
