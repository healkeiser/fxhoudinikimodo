from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time as _time
import uuid
from typing import Optional

import pathlib
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

log = logging.getLogger("kimodo_server")
logging.basicConfig(level=logging.INFO)

OUTPUT_DIR = pathlib.Path(os.environ.get("OUTPUT_DIR", "/workspace/output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEV_REFERENCE = OUTPUT_DIR / "dev_reference.npz"
MOCK_MODE = os.environ.get("MOCK_MODE", "1") == "1"

# Model to preload at startup; empty falls back to kimodo.DEFAULT_MODEL.
RESIDENT_MODEL = os.environ.get("KIMODO_MODEL", "")

_jobs: dict[str, dict] = {}

# Resident-mode state: a single-slot model cache and a lock that serialises
# inference (one GPU -> one generation at a time).
_model = None
_model_key: Optional[str] = None
_model_lock = asyncio.Lock()


def _ensure_model(name: str):
    """Load (or reuse) the resident Kimodo model. The cache key is the resolved
    canonical model name, so aliases (e.g. an empty preload default and the HDA's
    "Kimodo-SOMA-RP-v1.1") map to the same key and reuse the loaded model instead
    of reloading. Single slot: a different model replaces the previous one to
    bound VRAM to one model at a time."""
    global _model, _model_key
    import torch
    from kimodo import load_model
    from kimodo.model.registry import resolve_model_name

    key = resolve_model_name(name or "", default_family="Kimodo")
    if _model is not None and _model_key == key:
        return _model
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    log.info("[RESIDENT] loading model %s on %s ...", key, device)
    model, resolved = load_model(
        key, device=device, default_family="Kimodo", return_resolved_name=True
    )
    _model, _model_key = model, key
    log.info("[RESIDENT] model ready: %s", resolved)
    return model


def _build_initial_motion(cf, model):
    """Motion features for `continue_from`, so the first segment continues rather than starts.

    The NPZ carries 77-joint local rotations; the model works on its own smaller skeleton,
    and `from_SOMASkeleton77` is the exact inverse of the conversion used on output, so the
    round-trip is lossless. Root positions are skeleton independent.
    """
    if not cf:
        return None
    import torch
    skeleton = model.skeleton
    device = skeleton.device
    lr = torch.tensor(cf["local_rot_mats"], dtype=torch.float32, device=device)
    rp = torch.tensor(cf["root_positions"], dtype=torch.float32, device=device)
    if lr.ndim != 4 or rp.ndim != 2 or lr.shape[0] != rp.shape[0]:
        raise ValueError("continue_from needs local_rot_mats [n,J,3,3] and root_positions [n,3]")
    if lr.shape[1] != skeleton.nbjoints:
        lr = skeleton.from_SOMASkeleton77(lr)
    feats = model.motion_rep(lr[None], rp[None], to_normalize=False)
    log.info("[CONT] seeding %d frames of motion", feats.shape[1])
    return feats


def _build_constraints(constraints, model) -> list:
    """Turn the request's constraint dicts into Kimodo constraint objects.

    Standard Kimodo dicts (root2d / fullbody / end-effector with local axis-angle)
    go through load_constraints_lst. The bridge also accepts two "global" dict types
    authored from posed Houdini geometry \u2014 `fullbody-global` and `ee-global` \u2014 which
    carry global joint positions + rotation matrices and are built via the constraint
    constructors directly (the path Kimodo's own demo uses), avoiding any local /
    rest-pose convention round-trip on the client side."""
    if not constraints:
        return []
    import torch
    from kimodo.constraints import (
        load_constraints_lst, FullBodyConstraintSet, EndEffectorConstraintSet,
    )
    skeleton = model.skeleton
    device = skeleton.device
    # The HDA sends SOMA77 (77-joint) global data, but a SOMA-RP model constrains on
    # its smaller model skeleton (e.g. SOMASkeleton30). Map 77 -> the model joint set/
    # order the same way Kimodo's demo does (get_skel_slice against the 77 skeleton).
    src77 = getattr(skeleton, "somaskel77", None)
    skel_slice = (
        skeleton.get_skel_slice(src77)
        if src77 is not None and getattr(src77, "nbjoints", None) != skeleton.nbjoints
        else None
    )
    std, extra = [], []
    for c in constraints:
        t = c.get("type")
        if t in ("fullbody-global", "ee-global"):
            pos = torch.tensor(c["global_joints_positions"], dtype=torch.float32, device=device)
            rot = torch.tensor(c["global_joints_rots"], dtype=torch.float32, device=device)
            if skel_slice is not None and pos.shape[1] != skeleton.nbjoints:
                pos, rot = pos[:, skel_slice], rot[:, skel_slice]
            fi = torch.tensor(c["frame_indices"])
            sr = c.get("smooth_root_2d")
            sr = torch.tensor(sr, dtype=torch.float32, device=device) if sr else None
            if t == "fullbody-global":
                extra.append(FullBodyConstraintSet(skeleton, fi, pos, rot, smooth_root_2d=sr))
            else:
                extra.append(EndEffectorConstraintSet(
                    skeleton, fi, pos, rot, sr, joint_names=c["joint_names"]))
        else:
            std.append(c)
    return (load_constraints_lst(std, skeleton) if std else []) + extra


class _Progress:
    """Stand-in for tqdm: Kimodo wraps one denoising loop per segment with
    progress_bar(indices). Measured on the resident server, a segment costs roughly
    30 s of text encoding (CPU), ~8 s of denoising (GPU) and a few seconds of
    post-processing, so the fraction is (finished loops + position in the current
    loop) / segments, and `phase` says which of the three stages is running. Capped
    below 1 until the NPZ is written."""

    def __init__(self, job: dict, expected_loops: int):
        self.job, self.expected, self.done_loops = job, max(1, expected_loops), 0
        self.job["expected_loops"] = self.expected
        self._set_phase("encoding text")

    def _set_phase(self, phase: str) -> None:
        global _encode_est_seen
        now = _time.monotonic()
        if self.job.get("phase") == "encoding text":   # not on the first call, no phase yet
            _encode_est_seen = max(0.1, now - self.job["phase_started"])
        self.job["phase"] = phase
        self.job["phase_started"] = now

    def __call__(self, iterable, **_):
        items = list(iterable)
        n = max(1, len(items))
        self._set_phase(f"denoising segment {self.done_loops + 1}/{self.expected}")
        for i, it in enumerate(items):
            self.job["progress"] = min(0.99, (self.done_loops + i / n) / self.expected)
            yield it
        self.done_loops += 1
        self.job["progress"] = min(0.99, self.done_loops / self.expected)
        self._set_phase("post-processing" if self.done_loops >= self.expected else "encoding text")


def _infer_resident(req: "GenerateRequest", out_path: pathlib.Path, job: Optional[dict] = None) -> None:
    """Blocking in-process inference. Mirrors kimodo/scripts/generate.py main()."""
    from kimodo.exports.motion_io import save_kimodo_npz

    model = _ensure_model(req.model)
    texts, durations = req.texts_and_durations()
    num_frames = [max(1, int(d * model.fps)) for d in durations]
    constraint_lst = _build_constraints(req.constraints, model)
    initial_motion = _build_initial_motion(req.continue_from, model)
    progress = _Progress(job if job is not None else {}, expected_loops=len(texts))
    # Kimodo's multi-prompt path does not forward `progress_bar` to the sampling loop
    # (kimodo_model._multiprompt calls self._generate without it), so inject it there.
    # Inference is serialised by _model_lock, so patching the resident model is safe.
    orig_generate = model._generate

    def _generate_with_progress(*a, **k):
        k.setdefault("progress_bar", progress)
        return orig_generate(*a, **k)

    model._generate = _generate_with_progress
    try:
        output = model(
            texts,
            num_frames,
            num_denoising_steps=100,
            num_samples=1,
            multi_prompt=True,
            num_transition_frames=max(1, int(req.transition_frames)),
            initial_motion=initial_motion,
            post_processing=True,
            constraint_lst=constraint_lst,
            return_numpy=True,
            progress_bar=progress,
        )
    finally:
        del model._generate          # back to the class method
    n = int(output["posed_joints"].shape[0])
    single = {
        k: (v[0] if hasattr(v, "shape") and len(v.shape) > 0 and v.shape[0] == n else v)
        for k, v in output.items()
    }
    save_kimodo_npz(str(out_path), single)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not MOCK_MODE:
        # Preload the model now so the first /generate skips the load.
        await asyncio.to_thread(_ensure_model, RESIDENT_MODEL)
    yield


app = FastAPI(title="Kimodo API", lifespan=lifespan)


class GenerateRequest(BaseModel):
    prompt: str = ""
    duration: float = 3.0
    model: str = "soma-rp"
    force: bool = False          # bypass the cache and re-run inference
    # Continue an existing clip instead of starting fresh. Carries the tail of a clip
    # you already have as {"local_rot_mats": [n,77,3,3], "root_positions": [n,3]}; the
    # first requested segment then takes Kimodo's transition path and joins onto it,
    # and the returned NPZ is the new tail only (its first frames are the blended seam).
    continue_from: Optional[dict] = None
    constraints: Optional[list] = None   # Kimodo constraint dicts (type/frame_indices/...)
    # Multi-prompt timeline: ordered segments [{"prompt": str, "duration": seconds}, ...].
    # When given, `prompt`/`duration` are ignored and Kimodo blends consecutive segments
    # over `transition_frames` clip samples at each boundary.
    segments: Optional[list] = None
    transition_frames: int = 5

    def texts_and_durations(self) -> tuple[list, list]:
        if self.segments:
            texts = [str(s.get("prompt", "")).strip() for s in self.segments]
            durs = [float(s.get("duration", 0.0)) for s in self.segments]
            if not all(texts) or not all(d > 0 for d in durs):
                raise ValueError("Every segment needs a non-empty prompt and a duration > 0.")
            return texts, durs
        if not self.prompt.strip():
            raise ValueError("Either `prompt` or `segments` is required.")
        return [self.prompt], [float(self.duration)]


class JobStatus(BaseModel):
    job_id: str
    status: str          # queued | running | done | failed | cancelled
    npz_path: Optional[str] = None
    prompt: Optional[str] = None
    frames: Optional[int] = None
    joints: Optional[int] = None
    error: Optional[str] = None
    elapsed: Optional[float] = None
    cached: Optional[bool] = None    # True if served from a cached NPZ
    progress: Optional[float] = None  # 0..1 while running (denoising steps done / expected)
    phase: Optional[str] = None       # encoding text | denoising segment k/N | post-processing


def _cache_key(req: "GenerateRequest") -> str:
    """Identical requests share one NPZ. Single-prompt keys are unchanged from before the
    timeline existed; segment requests add their own fields."""
    payload = {"prompt": req.prompt, "duration": req.duration, "model": req.model,
               "constraints": req.constraints}
    if req.continue_from:
        payload["continue_from"] = req.continue_from
    if req.segments:
        payload["segments"] = req.segments
        payload["transition_frames"] = req.transition_frames
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _describe(req: "GenerateRequest") -> str:
    if req.segments:
        return " | ".join(str(s.get("prompt", "")) for s in req.segments)
    return req.prompt


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mock_mode": MOCK_MODE}


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    """Serve a finished job's NPZ over HTTP (used by the remote HDA, since a
    remote Houdini cannot read the server's filesystem via a path rewrite)."""
    if job_id not in _jobs:
        raise HTTPException(404, f"Job {job_id} not found.")
    npz = _jobs[job_id].get("npz_path")
    if not npz:
        raise HTTPException(409, f"Job {job_id} has no output (status={_jobs[job_id]['status']}).")
    path = pathlib.Path(npz).resolve()
    if not path.is_relative_to(OUTPUT_DIR.resolve()):
        raise HTTPException(403, "Output path is outside the output directory.")
    if not path.exists():
        raise HTTPException(404, "Output file is missing.")
    return FileResponse(str(path), media_type="application/octet-stream", filename=path.name)


@app.post("/generate", status_code=202)
async def generate(req: GenerateRequest) -> JobStatus:
    try:
        req.texts_and_durations()   # validate prompt/segments before queuing
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    desc = _describe(req)
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"status": "queued", "started_at": _time.monotonic(), "prompt": desc}
    asyncio.create_task(_run_job(job_id, req))
    log.info("[JOB] %s queued \u2014 prompt='%s'", job_id[:8], desc)
    return JobStatus(job_id=job_id, status="queued", prompt=desc)


# Only the denoising loop reports progress, and on CPU it is the short part of a segment:
# roughly 30 s of text encoding against ~8 s of denoising. Without this the bar jumps a
# whole segment then sits still. The creep is an estimate from elapsed time, capped short
# of the next real milestone so it never overtakes the truth.
#
# 30 s is the measured CPU figure and only a bootstrap: _encode_est_seen below replaces it
# with a real measurement after the first encode. Until then it can be wrong in two ways.
# Too high and the creep never gets going, which is what TEXT_ENCODER_DEVICE=cuda does, where
# encoding all but disappears and the bar behaves as it did before the creep existed. Too low
# is the worse direction: the ramp hits its ceiling early and sits there, the symptom this
# exists to fix. The encoder is a separate container, so the server cannot see which device
# it is on; hence an estimate rather than a branch.
_ENCODE_EST_S = max(0.1, float(os.environ.get("KIMODO_ENCODE_EST_S", "30")))

# What an encode actually took, the last time one finished on this server. It replaces the
# estimate above as soon as there is one, and it deliberately survives the job that measured
# it: a partial regeneration sends a single segment, so it has no earlier segment of its own
# to learn from, and by then a full Generate has usually already run. A plain float written by
# the inference thread and read by the event loop, so no lock.
_encode_est_seen: Optional[float] = None


def _elapsed(job: dict) -> float:
    """Seconds since the job was queued, to 0.1 s."""
    return round(_time.monotonic() - job.get("started_at", _time.monotonic()), 1)


def _cancelled(job: dict) -> bool:
    """True once a cancel has landed, stamping the elapsed time on the way out."""
    if job.get("status") != "cancelled":
        return False
    job["elapsed"] = _elapsed(job)
    return True


def _clip_shape(path) -> tuple[int, int]:
    """(samples, joints) of a generated NPZ."""
    with np.load(path) as z:
        t, j = z["posed_joints"].shape[:2]
    return int(t), int(j)


def _display_progress(job: dict) -> Optional[float]:
    prog, phase = job.get("progress"), job.get("phase")
    if prog is None or not phase or phase.startswith("denoising"):
        return prog
    est = _ENCODE_EST_S if _encode_est_seen is None else _encode_est_seen
    span = 1.0 / max(1, int(job.get("expected_loops", 1)))
    waited = _time.monotonic() - job.get("phase_started", _time.monotonic())
    return min(0.99, prog + span * 0.9 * min(1.0, waited / est))


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> JobStatus:
    if job_id not in _jobs:
        raise HTTPException(404, f"Job {job_id} not found.")
    job = _jobs[job_id]
    elapsed = job.get("elapsed")
    if elapsed is None and "started_at" in job:
        elapsed = _elapsed(job)
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        npz_path=job.get("npz_path"),
        prompt=job.get("prompt"),
        frames=job.get("frames"),
        joints=job.get("joints"),
        error=job.get("error"),
        elapsed=elapsed,
        cached=job.get("cached"),
        progress=_display_progress(job),
        phase=job.get("phase"),
    )


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> JobStatus:
    if job_id not in _jobs:
        raise HTTPException(404, f"Job {job_id} not found.")
    job = _jobs[job_id]
    if job["status"] not in ("queued", "running"):
        return job_status(job_id)
    # In-process inference can't be hard-interrupted: mark cancelled so a queued
    # job is skipped and a finished result is discarded.
    job["status"] = "cancelled"
    job["elapsed"] = _elapsed(job)
    log.info("[CANCEL] %s", job_id[:8])
    return job_status(job_id)


async def _run_job(job_id: str, req: GenerateRequest) -> None:
    job = _jobs[job_id]
    if _cancelled(job):                   # cancelled while still queued
        return
    job["status"] = "running"
    job["progress"] = 0.0

    try:
        if MOCK_MODE:
            if not DEV_REFERENCE.exists():
                job.update(status="failed", error=f"dev_reference.npz not found at {DEV_REFERENCE}")
                return
            log.info("[MOCK] %s \u2192 %s", job_id[:8], DEV_REFERENCE)
            T, J = _clip_shape(DEV_REFERENCE)
            job.update(status="done", npz_path=str(DEV_REFERENCE), frames=T, joints=J,
                       elapsed=_elapsed(job))
            return

        out_path = OUTPUT_DIR / f"{_cache_key(req)}.npz"

        if not req.force and out_path.exists():
            T, J = _clip_shape(out_path)
            log.info("[CACHE] %s \u2192 %s", job_id[:8], out_path.name)
            job.update(status="done", npz_path=str(out_path), frames=T, joints=J,
                       cached=True, elapsed=_elapsed(job))
            return

        # In-process inference, serialised on the single GPU. Cannot be hard-
        # cancelled mid-run; a cancel marks the job and the result is discarded.
        async with _model_lock:
            if _cancelled(job):
                return
            log.info("[GEN] %s prompt=%r", job_id[:8], _describe(req))
            await asyncio.to_thread(_infer_resident, req, out_path, job)
        if _cancelled(job):                   # cancelled while inference ran
            return

        elapsed = _elapsed(job)
        if not out_path.exists():
            job.update(status="failed", error="Output file not found after inference.", elapsed=elapsed)
            return

        T, J = _clip_shape(out_path)
        out_path.with_suffix(".json").write_text(json.dumps({
            "prompt": _describe(req), "duration": req.duration, "model": req.model,
            "segments": req.segments, "transition_frames": req.transition_frames,
            "frames": T, "joints": J, "created": _time.time(),
        }, indent=2))
        log.info("[DONE] %s \u2014 %d frames, %d joints, %.1fs", job_id[:8], T, J, elapsed)
        job.update(status="done", npz_path=str(out_path), frames=T, joints=J,
                   cached=False, elapsed=elapsed)
    except Exception as exc:  # never leave a job stuck in "running"
        if job.get("status") != "cancelled":
            log.exception("[FAIL] %s", job_id[:8])
            job.update(status="failed", error=str(exc)[-500:], elapsed=_elapsed(job))
