# Kimodo Timeline panel

A dockable Python Panel that edits a prompt timeline and constraint tracks on a
`vb::kimodo_motion` node. The node stays usable without it.

## Data on the node

Hidden string parm `timeline_json`:

```json
{
  "version": 1,
  "transition_frames": 5,
  "segments": [
    {"prompt": "a person walks forward", "frames": 72},
    {"prompt": "a person picks up a box", "frames": 48}
  ],
  "tracks": {
    "fullbody":  [1, 90],
    "LeftHand":  [],
    "RightHand": [60],
    "LeftFoot":  [],
    "RightFoot": []
  }
}
```

- `frames` are **scene frames** (the same unit as Duration). Segments are contiguous from
  Start Frame; segment *i* starts at `start_frame + sum(frames[:i])`.
- `tracks` hold scene-frame numbers at which the posed skeleton on **input 1** is sampled.
  `fullbody` pins the whole body; a limb track pins that joint only (`LeftHand`, `RightHand`,
  `LeftFoot`, `RightFoot`, Kimodo SOMA joint names).
- Empty `timeline_json` = legacy mode: Prompt + Duration + Pose Keyframes/Pose Constraint
  exactly as before. The panel seeds a timeline from those when first opened on a node.

Every panel edit writes `timeline_json` (and mirrors the total into `duration_frames`) inside
one undo group, so Ctrl+Z works and the .hip carries the timeline.

## HDA

- `timeline_json` hidden; **Open Timeline** button on the Generate tab.
- While `timeline_json` is non-empty: Duration, Pose Keyframes and Pose Constraint are
  disabled (the timeline owns them).
- Generate: with segments, sends `segments=[{prompt, duration_s}]` and `transition_frames`;
  each non-empty track becomes one constraint (`fullbody-global`, or `ee-global` with that
  joint), sampled from input 1 at the track's frames converted to clip sample indices.

## Server

`GenerateRequest` gains `segments: list[{prompt, duration}] | None` and
`transition_frames: int = 5`. Inference already runs `multi_prompt=True` with lists; segments
become `texts` / `num_frames`, and `num_transition_frames` is passed through. Cache key
includes both. Single-prompt requests are unchanged.

## Panel

`houdini/python_panels/kimodo_timeline.pypanel` + package `houdini/python/kimodo_timeline/`
(on `PYTHONPATH` via the Houdini package file).

- `model.py`: timeline arithmetic, no `hou`, no Qt. Tested by `tests/test_timeline_model.py`.
- `bridge.py`: read/write the node (`hou` only here).
- `widget.py`: PySide6 view.

Interactions:

| Where | Action | Result |
|---|---|---|
| Prompt block body | drag | reorder (drop index from cursor x) |
| Prompt block right edge | drag | resize (min 1 frame, frame-snapped) |
| Prompt block | double-click | edit text (multi-line dialog) |
| Prompt block / empty prompt row | right-click | add after / delete / edit |
| Track marker | drag | move (frame-snapped) |
| Track row | right-click | add key at cursor / delete key |
| Ruler | click or drag | set Houdini frame |
| Anywhere | wheel | zoom around the cursor |
| Anywhere | middle-drag | pan |
| Anywhere | F, Home, Fit button, double-click ruler | fit the whole timeline, with free room after the last segment |
| Footer | Transition spin, total label, Generate, live Status | |

The panel follows the current selection (first selected `vb::kimodo_motion*` node) and
shows an empty state otherwise.

### It is a Python Panel, so it sits inside a Houdini pane

`Canvas` is embedded in Houdini's own pane widget (`QOpenGLWidget/RE_WindowDrawable`),
which makes it the parent every unhandled Qt event propagates to. That is not true of a
floating window, and it is the one rule to keep in mind here:

**Consume presses and releases symmetrically.** `QWidget.mousePressEvent`'s default
implementation ignores the event, and an ignored press travels up the parent chain into
Houdini's pane. `mouseReleaseEvent` consumes every release, so handing a press to
`super()` delivered it to Houdini's pane with no release to match. That pane was then
left holding a button that never came up, and from then on every Houdini pane ignored
the mouse while this panel carried on working, until a restart.

`Canvas` sets `WA_NoMousePropagation` so this cannot come back, and
`tests/test_houdini_live.py::test_no_mouse_event_escapes_the_canvas` fails if it does.
The symptom looks exactly like a modal dialog, a leaked grab or a nested event loop, and
nine commits chased those before the event trace showed the real shape: one right-button
press reaching five receivers, its release reaching one. Reach for
`scripts/diagnose_input_wedge.py` (`trace()`, then read `%TEMP%/kimodo_wedge_trace.log`)
before theorising.

## Out of scope for v1

Thumbnails, per-key target editing inside the panel
(targets come from the posed rig on input 1, as today), root path track (input 0 curve).
