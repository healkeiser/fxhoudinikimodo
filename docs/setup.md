# Setup Guide


End-to-end setup, from the Docker inference server to the Houdini HDA. The server
preloads the Kimodo model once and serves inference in-process; Houdini fetches the
result over HTTP, so the server can run on this machine or a separate GPU box.

## Requirements

- **Docker Desktop** — Windows: WSL2 backend with GPU support enabled; Linux: Docker
  Engine + the NVIDIA Container Toolkit.
- **NVIDIA GPU, ≥ 4 GB VRAM** (≥ 6 GB recommended if the same GPU also drives Houdini).
  CPU-only inference is not supported by Kimodo.
- **~50 GB free disk** — the built `kimodo:1.0` image alone is ~35 GB, plus model weights
  and the HuggingFace cache (the Llama-based text encoder is 16 GB).
- **HuggingFace account + read token** — the Kimodo weights themselves are ungated, but
  the text encoder is built on Meta's gated `meta-llama/Meta-Llama-3-8B-Instruct`. Request
  access there (fill the form with a real affiliation; a `-` gets rejected, and a rejection
  is final per account). If Meta declines, see step 4b.
- **Houdini 20.5+** (developed on 22.0.368).

> **VRAM stays occupied while the server runs.** The `api` container preloads the
> model and keeps it resident in VRAM (~3–4 GB) for its whole lifetime — it is **not**
> freed between generations. Stop it when you're done generating to reclaim the GPU
> (e.g. before a heavy Houdini / Karma render):
> `docker compose -f docker-compose.bridge.yaml stop api`

## 1. Environment validation

```bash
git clone https://github.com/<your-org>/kimodo-houdini-bridge.git
cd kimodo-houdini-bridge
python scripts/check_env.py
```

The script checks Docker, GPU pass-through, RAM and port availability. Fix any
`FAIL` items before continuing.

## 2. Clone Kimodo & build the Docker image

```bash
# Clone Kimodo (e.g. alongside this repo)
git clone https://github.com/nv-tlabs/kimodo.git
cd kimodo
git clone https://github.com/nv-tlabs/kimodo-viser.git   # required before building
```

**Windows only:** fix shell-script line endings before building:

```bash
sed -i 's/\r//' kimodo/scripts/docker-entrypoint.sh
```

```bash
docker build -t kimodo:1.0 .   # first build downloads the ~10 GB base image
```

## 3. Deploy the bridge into the kimodo dir

The server runs from the kimodo repo (it needs the `kimodo` package on `/workspace`).
Copy the two bridge files in — the compose file has a distinct name so it sits next
to Kimodo's own `docker-compose.yaml`:

```bash
cp /path/to/fxhoudinikimodo/kimodo_server.py .
cp /path/to/fxhoudinikimodo/docker-compose.bridge.yaml .
mkdir -p output
export HUGGING_FACE_HUB_TOKEN=$(cat ~/.cache/huggingface/token)   # or paste your hf_... token
```

**Windows only:** the compose file mounts `${HOME}/.cache/huggingface`, and `HOME` is not
set on Windows, so downloads would land inside the Docker VM and vanish. Pin the cache
with a `.env` next to the compose file:

```
HF_HOME=C:/Users/<you>/.cache/huggingface
```

In PowerShell, read the token without echoing it:
`$env:HUGGING_FACE_HUB_TOKEN = (Get-Content "$env:USERPROFILE\.cache\huggingface\token" -Raw).Trim()`.
Notepad likes to save the token file as `token.txt`; the name must be exactly `token`.

## 4. Cache the model weights (one time)

The server loads the model from the local HuggingFace cache **offline** at startup
(`HF_HUB_OFFLINE=1`), so the weights must be downloaded once first:

```bash
docker compose -f docker-compose.bridge.yaml run --rm --no-deps -e HF_HUB_OFFLINE=0 api \
  hf download nvidia/Kimodo-SOMA-RP-v1.1
```

(`huggingface-cli` was renamed `hf` in recent `huggingface_hub` releases. Repeat for any
other model you select in the node, e.g. `nvidia/Kimodo-SOMA-SEED-v1.1`.)

Large downloads through the Docker bind mount can stall on Windows. Pull them on the host
instead; they land in the same cache:

```powershell
$env:HF_HOME = "$env:USERPROFILE\.cache\huggingface"
uv tool run --from huggingface_hub hf download nvidia/Kimodo-SOMA-RP-v1.1
```

### 4b. If Meta rejected your Llama request

The text encoder is McGill's LLM2Vec adapter on top of Llama 3 8B Instruct; the adapter
repo is MIT and ungated, only Meta's base weights are gated. Identical weights exist as
ungated mirrors (e.g. `NousResearch/Meta-Llama-3-8B-Instruct`, which ships Meta's LICENSE
and USE_POLICY; the Llama 3 Community License still applies to you). To use one:

```powershell
# from the kimodo dir
foreach ($r in 'LLM2Vec-Meta-Llama-3-8B-Instruct-mntp','LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised') {
  docker compose -f docker-compose.bridge.yaml run --rm --no-deps -e HF_HUB_OFFLINE=0 api `
    hf download McGill-NLP/$r --local-dir /workspace/text_encoders/McGill-NLP/$r
}
# point both adapters at the mirror
(Get-ChildItem text_encoders\McGill-NLP\*\adapter_config.json) | ForEach-Object {
  (Get-Content $_ -Raw) -replace '"meta-llama/Meta-Llama-3-8B-Instruct"','"NousResearch/Meta-Llama-3-8B-Instruct"' | Set-Content $_ -NoNewline }
uv tool run --from huggingface_hub hf download NousResearch/Meta-Llama-3-8B-Instruct
```

The shipped `docker-compose.bridge.yaml` already passes `TEXT_ENCODERS_DIR=/workspace/text_encoders`
to both containers, so Kimodo loads the adapters from that folder and never asks Meta.

## 5. Start the services

```bash
docker compose -f docker-compose.bridge.yaml up text-encoder -d   # wait until "healthy"
docker compose -f docker-compose.bridge.yaml ps

# Live inference (preloads the model — watch the log for "Application startup complete")
MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
curl http://localhost:8001/health     # {"status":"ok","mock_mode":false}
```

> **Mock mode** (`MOCK_MODE=1`, the default) skips inference and returns
> `output/dev_reference.npz` — handy for HDA development without a GPU.
>
> **Changing the port:** the API defaults to **8001** (port 8000 is reserved by Docker
> Desktop on Windows). If 8001 is already in use, set `KIMODO_PORT` when starting the
> api and point the node's **API Server URL** at the same port:
> ```bash
> KIMODO_PORT=8002 MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
> # then in the HDA: API Server URL = http://localhost:8002
> ```

## 6. Houdini Python packages

```bash
# Linux / macOS
$HFS/bin/hython -m pip install requests scipy numpy
# Windows (adjust the HFS path)
"C:\Program Files\Side Effects Software\Houdini 22.0.xxx\bin\hython.exe" -m pip install requests scipy numpy
```

## 7. Install & use the HDA

The repo ships the HDA prebuilt under `houdini/otls/` — install it in Houdini
(**Assets → Install Asset Library…** or via the package file; see
[houdini/README.md](../houdini/README.md)).

Drop a **`kimodo_motion`** node in a SOP network:

1. Server tab: set **API Server URL** to `http://localhost:8001` (or the GPU host) and press
   **Test Connection**; the status under the node should read `Server OK`.
2. Generate tab: set **Prompt** and **Duration (frames)**, press **Generate**. The status under
   the node goes Queued → Running → Done; the NPZ downloads to **Download Dir**
   (`$HIP/kimodo_cache`) and the node cooks.
3. The node has four outputs in SideFX character order — **Rest Geometry** (skinned body
   mesh), **Capture Pose**, **Animated Pose** and **T-Pose**. The clip starts on **Start
   Frame** and is retimed to your scene FPS by default (Output tab).
4. To deform the body, add a **`kinefx::jointdeform`** and wire outputs 0, 1, 2 straight
   into inputs 0, 1, 2.

> **Constraints (optional):** to steer the motion toward spatial targets, supply
> [Kimodo constraints](https://research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/constraints.html)
> as JSON (**Constraints File** / **Constraints JSON**), connect a curve/points to input 0
> for a root path, or use **Create Pose Rig** to pose a skeleton (input 1) for full-body /
> end-effector constraints — see [houdini/README.md](../houdini/README.md#constraints-optional).
>
> Developers who edit the cook scripts can rebuild the HDA:
> `hython scripts/build_skin.py` → `hython scripts/create_hda.py` →
> `hython scripts/_add_help.py` — see [houdini/README.md](../houdini/README.md#rebuilding-the-hda).
