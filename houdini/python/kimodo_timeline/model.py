"""Timeline data model: prompt segments plus constraint tracks. No hou, no Qt.

Frames are scene frames. Segment i starts at ``start + sum(frames[:i])``.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field

VERSION = 1
TRACKS = ("fullbody", "LeftHand", "RightHand", "LeftFoot", "RightFoot")
TRACK_LABELS = {
    "fullbody": "Full Body",
    "LeftHand": "L Hand",
    "RightHand": "R Hand",
    "LeftFoot": "L Foot",
    "RightFoot": "R Foot",
}
MIN_FRAMES = 1


@dataclass
class Segment:
    prompt: str = ""
    frames: int = 24

    def clamp(self) -> Segment:
        self.frames = max(MIN_FRAMES, int(self.frames))
        return self


@dataclass
class Timeline:
    segments: list[Segment] = field(default_factory=list)
    transition_frames: int = 5
    tracks: dict[str, list[int]] = field(
        default_factory=lambda: {t: [] for t in TRACKS}
    )

    ###### Queries
    @property
    def total_frames(self) -> int:
        return sum(s.frames for s in self.segments)

    def starts(self, start_frame: int = 1) -> list[int]:
        """Scene frame on which each segment begins."""
        out, f = [], start_frame
        for s in self.segments:
            out.append(f)
            f += s.frames
        return out

    ###### Segment edits
    def add(self, prompt: str, frames: int, after: int | None = None) -> int:
        seg = Segment(prompt, frames).clamp()
        idx = len(self.segments) if after is None else after + 1
        self.segments.insert(idx, seg)
        return idx

    def remove(self, index: int) -> None:
        del self.segments[index]

    def resize(self, index: int, frames: int) -> int:
        self.segments[index].frames = frames
        return self.segments[index].clamp().frames

    def move(self, src: int, dst: int) -> int:
        """Reorder: move segment ``src`` so it ends up at index ``dst``."""
        n = len(self.segments)
        dst = max(0, min(dst, n - 1))
        if src == dst:
            return dst
        seg = self.segments.pop(src)
        self.segments.insert(dst, seg)
        return dst

    def set_prompt(self, index: int, prompt: str) -> None:
        self.segments[index].prompt = prompt

    ###### Track edits
    def add_key(self, track: str, frame: int) -> None:
        keys = self.tracks.setdefault(track, [])
        if frame not in keys:
            keys.append(int(frame))
            keys.sort()

    def remove_key(self, track: str, frame: int) -> None:
        keys = self.tracks.get(track, [])
        if frame in keys:
            keys.remove(frame)

    def move_key(self, track: str, old: int, new: int) -> int:
        """Move a key; if ``new`` is taken it stays put. Returns its frame."""
        keys = self.tracks.get(track, [])
        if old not in keys or (new in keys and new != old):
            return old
        keys[keys.index(old)] = int(new)
        keys.sort()
        return int(new)

    def clamp_keys(self, start_frame: int = 1) -> None:
        """Drop keys outside [start, end] after a resize/remove."""
        end = start_frame + self.total_frames - 1
        for t, keys in self.tracks.items():
            self.tracks[t] = sorted(k for k in keys if start_frame <= k <= end)

    ###### Serialisation
    def to_json(self) -> str:
        return json.dumps(
            {
                "version": VERSION,
                "transition_frames": int(self.transition_frames),
                "segments": [
                    {"prompt": s.prompt, "frames": int(s.frames)}
                    for s in self.segments
                ],
                "tracks": {
                    t: [int(k) for k in self.tracks.get(t, [])] for t in TRACKS
                },
            },
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> Timeline:
        if not text or not text.strip():
            return cls()
        d = json.loads(text)
        tl = cls(
            segments=[
                Segment(s.get("prompt", ""), s.get("frames", 24)).clamp()
                for s in d.get("segments", [])
            ],
            transition_frames=int(d.get("transition_frames", 5)),
        )
        tracks = d.get("tracks", {})
        tl.tracks = {
            t: sorted(int(k) for k in tracks.get(t, [])) for t in TRACKS
        }
        return tl

    @classmethod
    def from_legacy(
        cls, prompt: str, frames: int, pose_keyframes: str = ""
    ) -> Timeline:
        """Seed a timeline from the single-prompt parms of an older node."""
        tl = cls(segments=[Segment(prompt or "", frames).clamp()])
        for tok in (pose_keyframes or "").replace(",", " ").split():
            with contextlib.suppress(ValueError):
                tl.add_key("fullbody", int(tok))
        return tl

    def request_segments(self, fps: float) -> list[dict]:
        return [
            {"prompt": s.prompt.strip(), "duration": s.frames / float(fps)}
            for s in self.segments
        ]
