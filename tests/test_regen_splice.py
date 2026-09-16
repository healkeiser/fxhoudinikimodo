"""Checks for kimodo_timeline.regen's pure functions. Plain python: python
tests/test_regen_splice.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "houdini" / "python")
)

from kimodo_timeline.regen import (  # noqa: E402
    cut_sample,
    samples_for,
    splice,
)

# scene rates a shot gets cut at, against the 30 the SOMA models generate at,
# plus a couple of mismatched pairs to keep the rounding honest
FPS_PAIRS = ((24, 30), (25, 30), (30, 30), (24, 20), (60, 30))


def test_cut_lines_up_with_what_the_server_generates():
    """The slot a segment occupies in the clip has to be exactly as long as
    the segment the server generates for it.

    The server's length is `int(duration * fps)` per segment, which is what
    `samples_for` mirrors. If the cut is computed any other way the splice
    drops a sample, the tail pin lands off by one, and every later
    single-segment regen drops another.
    """
    for scene, source in FPS_PAIRS:
        for f in range(4, 120):
            frames = [f, f + 1, f + 7, f + 13]
            for i in range(1, len(frames)):
                cut = cut_sample(frames, i, scene, source)
                resume = cut_sample(frames, i + 1, scene, source)
                nf = samples_for(frames[i], scene, source)
                assert resume - cut == nf, (
                    "%g -> %g fps, segment %d of %s: slot is %d samples, "
                    "server generates %d"
                    % (scene, source, i, frames, resume - cut, nf)
                )


def _clip(length):
    """Per-sample arrays whose values say which sample they came from, so a
    misplaced join shows up as a wrong number rather than a right shape."""
    col = np.arange(length, dtype=float).reshape(length, 1)
    return {
        k: np.repeat(col, 3, axis=1) for k in ("posed_joints", "root_positions")
    }


def test_single_segment_splice_keeps_the_clip_length():
    scene, source, n = 24, 30, 5
    frames = [31, 47, 23]
    have = cut_sample(frames, len(frames), scene, source)
    old = _clip(have)

    cut = cut_sample(frames, 1, scene, source)
    resume = cut_sample(frames, 2, scene, source)
    nf = samples_for(frames[1], scene, source)
    # what the server returns: the blended transition, then the segment
    new = {k: np.full((n + nf, 3), -1.0) for k in old}

    merged = splice(old, new, cut, n, resume_at=resume)
    assert merged["posed_joints"].shape[0] == have
    # everything either side of the regenerated segment is untouched
    assert np.array_equal(
        merged["posed_joints"][: cut - n], old["posed_joints"][: cut - n]
    )
    assert np.array_equal(
        merged["posed_joints"][cut + nf :], old["posed_joints"][resume:]
    )


def test_run_to_the_end_splice_drops_the_old_tail():
    scene, source, n = 24, 30, 5
    frames = [31, 47, 23]
    have = cut_sample(frames, len(frames), scene, source)
    old = _clip(have)

    cut = cut_sample(frames, 1, scene, source)
    rest = have - cut  # both remaining segments, regenerated together
    new = {k: np.full((n + rest, 3), -1.0) for k in old}

    merged = splice(old, new, cut, n, resume_at=None)
    assert merged["posed_joints"].shape[0] == have


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
