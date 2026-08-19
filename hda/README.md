# HDA Library

[繁體中文](README.zh-TW.md)

This directory contains the Houdini Digital Asset for the Kimodo-Houdini bridge,
stored in **unpacked (VCS-friendly) format** — a directory ending in `.hda/`.

---

## kimodo_motion.hda

**Type:** `Sop/kimodo_motion`  
**Context:** SOP (geometry network)  
**Houdini:** H20.5+

A SOP node that generates 3D human motion from a natural language prompt via the
[NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) model. It sends the prompt to a
running `kimodo_server`, downloads the resulting NPZ over HTTP, and rebuilds the
77-joint SOMA motion as KineFX-compatible geometry — animated skeleton, rest
skeletons, and a skinned body mesh.

### Outputs

Four outputs. World rotations use Houdini's row-vector / row-major convention
(transposed from Kimodo's column-vector matrices). Skeletons connect each
parent-child joint pair with a polyline primitive.

| # | Label | Content |
|---|-------|---------|
| 0 | **Animated Pose** | Per-frame animated skeleton. `name`, `path`, `parent_id`, `transform` (float[9] world rotation), `localtransform` (float[16] local 4×4), and `contact` (int 0/1) when the NPZ carries foot contacts — see [Foot contacts](#foot-contacts). |
| 1 | **Capture Pose** | The A-pose rest skeleton the body mesh is bound to (feet on floor). `name`, `transform`. |
| 2 | **Rest Geometry** | The SOMA77 body mesh in its bind pose, with a KineFX `boneCapture` attribute (weights + bind from Kimodo's skinning). |
| 3 | **T-Pose** | A T-pose skeleton (`name`, `transform`) for reference / retargeting. |

**To deform the body**, drop a **`kinefx::jointdeform`** (Labs/KineFX Joint Deform) and wire:
input 0 = **Rest Geometry** (output 2), input 1 = **Capture Pose** (output 1),
input 2 = **Animated Pose** (output 0). The mesh follows the animation and returns to
the bind pose at rest. (output 0 also drives a **Rig Pose / Bone Deform** workflow directly.)

### Inputs (optional)

The node has two **optional inputs**, both for authoring constraints from geometry
instead of JSON (see [Constraints](#constraints-optional)); leave them unconnected for
normal use:

- **Input 0 — Root Path / Waypoints**: a curve or points → a `root2d` constraint.
- **Input 1 — Pose Keyframes (skeleton)**: a posed SOMA77 skeleton → a full-body or
  end-effector constraint (see [Pose constraints](#pose-constraints-input-1)).

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| API Server URL | `http://localhost:8001` | URL of the running `kimodo_server`. Point at the GPU host if the server runs elsewhere. |
| Download Dir | `$HIP/kimodo_cache` | Local folder where finished NPZ files are downloaded from the server over HTTP. |
| Prompt | `a person walks forward` | Natural language description of the motion. Be specific about body part, direction, speed and style. |
| Duration (s) | `3.0` | Length of the clip in seconds. At 30 fps, 3 s = 90 frames. |
| Model | `Kimodo-SOMA-RP-v1.1` | Kimodo model variant. `RP` conditions on a rest pose; `SEED` uses a fixed seed for reproducibility. |
| Force Regenerate | `off` | Bypass the server cache and re-run inference even if a matching clip exists. |
| Constraints File | _(empty)_ | Optional [Kimodo constraints](https://research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/constraints.html) JSON file (`*.json`). See [Constraints](#constraints-optional). |
| Constraints JSON | _(empty)_ | Optional inline constraints JSON; takes precedence over Constraints File. |
| Pose Constraint | `Full-Body` | How to use a posed skeleton on input 1: `Full-Body` (whole skeleton) or `End-Effector`. See [Pose constraints](#pose-constraints-input-1). |
| Left/Right Hand/Foot | Right Hand | For `End-Effector`: which joints to pin (the rest of the body stays free). |
| **Create Pose Rig** | — | Drops an independent A-pose rig (+ Rig Pose) into the network and wires it to input 1, ready to pose. |
| Pose Keyframes | _(empty)_ | Frames to sample input 1 at, e.g. `0 45 89`. Empty = no pose constraint. |
| **Generate** | — | Submits the prompt to `<API Server URL>/generate` and returns immediately with a job ID. A background thread polls for progress, so Houdini stays responsive. When done, the NPZ is downloaded to **Download Dir**, **NPZ Path** is set, and the node recooks. |
| **Cancel** | — | Cancels the job. A resident server can't interrupt an already-running generation — Cancel stops a queued job or discards the result. |
| Status | _(read-only)_ | Live job state: `Queued`, `Running... (Ns)`, `Downloading...`, `Done (Ns)`, `Failed`, `Cancelled`. |
| NPZ Path | _(empty)_ | The `.npz` the node reads to build the skeleton — a **file field** (browse, `*.npz`). Set by Generate; you can also point it at any compatible NPZ by hand (no Generate/server needed). Empty = empty geometry until set. |

#### What is an NPZ file?

An NPZ file (NumPy compressed archive) is Kimodo's inference output. The node reads:

| Key | Shape | Content |
|-----|-------|---------|
| `posed_joints` | `(T, 77, 3)` | World-space joint positions in metres — **read by the node** (joint placement) |
| `global_rot_mats` | `(T, 77, 3, 3)` | World-space joint rotations — **read by the node**; `transform` / `localtransform` are derived from these |
| `local_rot_mats` | `(T, 77, 3, 3)` | Local rotation matrices (Kimodo output; not required by the node) |
| `root_positions` | `(T, 3)` | Root (Hips) world position |
| `foot_contacts` | `(T, 6)` | Boolean foot-contact labels — **read by the node** when present; becomes `contact` on output 0 |

The node only needs **`posed_joints`** and **`global_rot_mats`** (SOMA77 joint order) to
rebuild the skeleton. Any compatible NPZ works regardless of how it was produced — set
**NPZ Path** to it. **Download Dir** is only used by **Generate**.

#### Foot contacts

When the NPZ carries `foot_contacts`, output 0 gains an `int` point attribute
**`contact`**: `1` on a foot joint while it is planted that frame, `0` everywhere else
(including every non-foot joint). NPZs without the key simply have no `contact`
attribute — check for it rather than assuming zeros mean "never planted".

The labels come from the model itself: Kimodo predicts a contact channel and
thresholds it at 0.5, so they are not re-derived from the joint motion here. Kimodo
also consumes them internally to clean up foot skating before the motion is written.

Contacts are detected on **two joints per side** — the ankle and the toe base. A
SOMA77 NPZ reports six channels, where `LeftToeEnd` / `RightToeEnd` are copies of the
matching `ToeBase` channel rather than independent detections (a four-channel NPZ —
Kimodo's internal representation, without the SOMA77 expansion — is read too; see
below):

| Channel | Joint | |
|---|---|---|
| 0 | `LeftFoot` | detected |
| 1 | `LeftToeBase` | detected |
| 2 | `LeftToeEnd` | copy of channel 1 |
| 3 | `RightFoot` | detected |
| 4 | `RightToeBase` | detected |
| 5 | `RightToeEnd` | copy of channel 4 |

Every joint carries the attribute either way — it is written on all 77 points, not
only the feet — so treat the two `ToeEnd` joints as redundant when driving a foot
lock. With a **four-channel** NPZ the two `ToeEnd` joints are not covered by any
channel and stay `0` for the whole clip, which is indistinguishable from "never
planted"; use `ToeBase` in that case.

Typical uses: locking a foot in place while `contact == 1` to kill foot skating, or
detecting the `0` → `1` transition as a footstep event to drive dust, decals or audio
cues.

### Constraints (optional)

[Kimodo constraints](https://research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/constraints.html)
steer the generated motion to hit spatial targets: a root 2D path or waypoints,
full-body keyframes, or end-effector (hand/foot) targets. Supply a JSON **list of
constraint dicts**, where each dict's `type` field selects the constraint kind.

**Constraints File** and **Constraints JSON** are not constraint types — they are two
ways to provide that *same* JSON payload: point **Constraints File** at a `*.json`
(e.g. one exported from the Kimodo demo), or paste the JSON into **Constraints JSON**.
The inline JSON wins when non-empty; otherwise the file is read. One list can hold
several constraints of different types. Leave both empty for unconstrained generation.
(Input geometry, if connected, adds a `root2d` on top — see below.)

Targets use Kimodo's coordinate space: **Y-up, metres, +Z forward, root at XZ = (0, 0)
on frame 0** — the same world space this node outputs. The simplest type is a `root2d`
waypoint set (`frame_indices` + `[x, z]` pairs):

```json
[{"type": "root2d", "frame_indices": [0, 90], "smooth_root_2d": [[0, 0], [2, 1]]}]
```

Other types (`fullbody`, `left-hand`/`right-hand`/`left-foot`/`right-foot`) also need
per-joint rotations; author them in the Kimodo demo and export the JSON. Constraints are
part of the cache key, so a new constraint set triggers a fresh generation.

**Authoring a `root2d` from geometry (input 0):** instead of writing JSON, connect
geometry to the node's optional input. Each point's world XZ becomes a `smooth_root_2d`
target (Houdini XZ maps 1:1 to Kimodo's space — you can trace over the node's own output
trajectory). Points carrying an integer `frame` point attribute become **sparse waypoints**
at those frames (you control the timing); otherwise the points, in order (e.g. a resampled
polyline), are spread evenly across the clip as a **denser path**. The geometry-derived
`root2d` is appended to any JSON constraints above.

#### Pose constraints (input 1)

Author **full-body** or **end-effector** constraints by posing a skeleton, instead of
writing the per-joint JSON by hand:

1. Press **Create Pose Rig**. It drops an **independent A-pose rig** (a `kimodo_pose_rig`
   source + a KineFX **Rig Pose**) into the network and wires it into the node's
   **input 1** — nothing is wired from this node's own outputs, so there's no loop.
2. Pose / keyframe that rig (on the Rig Pose) to define the poses you want to pin.
3. Set **Pose Keyframes** to the frames to sample (e.g. `0 45 89`) and choose **Pose
   Constraint** = Full-Body or End-Effector (for the latter, toggle which Hand/Foot to
   pin — the rest of the body stays free).

At each keyframe the node reads the posed joints' world positions and rotations and sends
them to the server, which builds a `FullBodyConstraintSet` / `EndEffectorConstraintSet`
directly (the same path Kimodo's demo uses). The conversion inverts the node's own forward
transform, so the rig **must be one Create Pose Rig produced** (it loads the same A-pose in
the expected rotation convention); an arbitrary rig won't convert correctly.

### Caching

The server caches results by a SHA-256 hash of `prompt + duration + model + constraints`.
Re-running with identical settings returns instantly (Status shows `Done (cached)`); enable
**Force Regenerate** to bypass it.

### Prerequisites

A running `kimodo_server`. From the kimodo dir (see [Setup Guide](../docs/setup.md)):

```bash
docker compose -f docker-compose.bridge.yaml up text-encoder -d   # wait until healthy
MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
```

The model is preloaded from the local HuggingFace cache (offline), so the weights must
be cached first (`huggingface-cli download nvidia/Kimodo-SOMA-RP-v1.1`). It then stays
resident in VRAM while the api container runs — stop it to free VRAM.

---

## Rebuilding the HDA

If you edit the cook scripts or skinning, regenerate the HDA:

```bash
# 1. Build the embedded skin geometry (mesh + capture, A-pose skeleton)
hython scripts/build_skin.py

# 2. Rebuild the packed HDA at the repo root (embeds the skin sections)
hython scripts/create_hda.py

# 3. Add the help card and save the unpacked HDA to hda/
hython scripts/_add_help.py
```

---

## Installing in Houdini

### Option A — Houdini Package (recommended)

Copy the package file to your Houdini packages directory, then edit
`KIMODO_BRIDGE_ROOT` to the absolute path of this repo:

```bash
# Windows
copy kimodo-houdini-bridge.json %HOUDINI_USER_PREF_DIR%\packages\
# Linux / macOS
cp kimodo-houdini-bridge.json ~/houdiniXX.Y/packages/
```

Restart Houdini — the `kimodo_motion` SOP appears in the Tab menu under **Kimodo**.

### Option B — Manual install

In Houdini: **Assets → Install Asset Library…** → select `hda/kimodo_motion.hda/`.
