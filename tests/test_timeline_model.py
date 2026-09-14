"""Checks for kimodo_timeline.model. Plain python: python tests/test_timeline_model.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "houdini", "python"))

from kimodo_timeline.model import Timeline, Segment, TRACKS  # noqa: E402


def test_totals_and_starts():
    tl = Timeline([Segment("walk", 72), Segment("box", 48)])
    assert tl.total_frames == 120
    assert tl.starts(1) == [1, 73]
    assert tl.segment_at(1) == 0 and tl.segment_at(72) == 0 and tl.segment_at(73) == 1
    assert tl.segment_at(121) is None
    assert tl.seconds(24) == [3.0, 2.0]
    assert tl.request_segments(30)[0]["duration"] == 2.4


def test_resize_never_below_one_frame():
    tl = Timeline([Segment("a", 10)])
    assert tl.resize(0, -5) == 1
    assert tl.resize(0, 7) == 7


def test_add_remove_move():
    tl = Timeline([Segment("a", 10), Segment("b", 10), Segment("c", 10)])
    assert tl.add("x", 5, after=0) == 1
    assert [s.prompt for s in tl.segments] == ["a", "x", "b", "c"]
    tl.remove(1)
    assert tl.move(0, 2) == 2
    assert [s.prompt for s in tl.segments] == ["b", "c", "a"]
    assert tl.move(0, 99) == 2          # clamped to last index
    assert [s.prompt for s in tl.segments] == ["c", "a", "b"]


def test_keys():
    tl = Timeline([Segment("a", 48)])
    tl.add_key("LeftHand", 30); tl.add_key("LeftHand", 10); tl.add_key("LeftHand", 30)
    assert tl.tracks["LeftHand"] == [10, 30]
    assert tl.move_key("LeftHand", 10, 30) == 10       # target taken: stays
    assert tl.move_key("LeftHand", 10, 20) == 20
    tl.remove_key("LeftHand", 30)
    tl.add_key("fullbody", 100)
    tl.clamp_keys(1)                                    # clip is frames 1..48
    assert tl.tracks["fullbody"] == [] and tl.tracks["LeftHand"] == [20]


def test_json_roundtrip_and_legacy():
    tl = Timeline([Segment("walk", 72), Segment("box", 48)], transition_frames=8)
    tl.add_key("RightFoot", 60)
    back = Timeline.from_json(tl.to_json())
    assert back.to_json() == tl.to_json()
    assert set(back.tracks) == set(TRACKS)
    assert Timeline.from_json("").segments == []
    leg = Timeline.from_legacy("a person waves", 72, "1, 45 89")
    assert leg.segments[0].frames == 72 and leg.tracks["fullbody"] == [1, 45, 89]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
