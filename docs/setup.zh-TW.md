# 安裝指南

> **Note (English):** this translation describes the upstream v1.0 interface of chordee/kimodo-houdini-bridge. The fxhoudinikimodo changes (output order, tabs, frame-based duration, retiming, Windows notes) are documented in the English files only.

[English](setup.md)

從 Docker 推論伺服器到 Houdini HDA 的完整流程。伺服器在啟動時預載 Kimodo 模型、
in-process 推論;Houdini 透過 HTTP 取回結果,所以伺服器可在本機或另一台 GPU 機器。

## 需求

- **Docker Desktop** —— Windows:WSL2 後端並啟用 GPU 支援;Linux:Docker Engine + NVIDIA Container Toolkit。
- **NVIDIA GPU,≥ 4 GB VRAM**(若同一張卡也要跑 Houdini,建議 ≥ 6 GB)。Kimodo 不支援純 CPU 推論。
- **約 20 GB 可用硬碟** —— CUDA base image(約 10 GB)加上建好的 `kimodo:1.0` image、模型權重與 HuggingFace 快取。
- **HuggingFace 帳號 + access token** —— 下載 Kimodo 模型權重需要。請先到模型的 HuggingFace 頁面接受授權條款,再建立 token。
- **Houdini 20.5+**。

> **伺服器執行期間會持續佔用 VRAM。** `api` 容器啟動時預載模型,並在整個執行期間
> 把模型常駐在 VRAM(約 3–4 GB)——**不會**在每次生成之間釋放。生成完要把它停掉以釋放
> GPU(例如在跑吃重的 Houdini / Karma 算圖前):
> `docker compose -f docker-compose.bridge.yaml stop api`

## 1. 環境檢查

```bash
git clone https://github.com/<your-org>/kimodo-houdini-bridge.git
cd kimodo-houdini-bridge
python scripts/check_env.py
```

檢查 Docker、GPU pass-through、記憶體與埠號。有 `FAIL` 先修掉再繼續。

## 2. Clone Kimodo 並建置 Docker image

```bash
# Clone Kimodo（例如放在本 repo 旁邊）
git clone https://github.com/nv-tlabs/kimodo.git
cd kimodo
git clone https://github.com/nv-tlabs/kimodo-viser.git   # 建置前必須先 clone
```

**僅 Windows：** 建置前先修 shell 腳本換行:

```bash
sed -i 's/\r//' kimodo/scripts/docker-entrypoint.sh
```

```bash
docker build -t kimodo:1.0 .   # 首次建置會下載約 10 GB 的 base image
```

## 3. 把 bridge 部署進 kimodo 目錄

伺服器從 kimodo repo 執行(需要 `kimodo` 套件在 `/workspace`)。把兩個 bridge 檔複製進去
——compose 檔取了不同的名字,好跟 Kimodo 自己的 `docker-compose.yaml` 並存:

```bash
cp /path/to/kimodo-houdini-bridge/kimodo_server.py .
cp /path/to/kimodo-houdini-bridge/docker-compose.bridge.yaml .
mkdir -p output
export HUGGING_FACE_HUB_TOKEN=$(cat ~/.cache/huggingface/token)   # 或貼上你的 hf_... token
```

## 4. 預先快取模型權重(一次性)

伺服器啟動時從本機 HuggingFace 快取**離線**載入模型(`HF_HUB_OFFLINE=1`),所以權重要先下載一次:

```bash
docker compose -f docker-compose.bridge.yaml run --rm --no-deps api \
  huggingface-cli download nvidia/Kimodo-SOMA-RP-v1.1
```

(若節點會選別的模型,一併下載,例如 `nvidia/Kimodo-SOMA-SEED-v1.1`。)

## 5. 啟動服務

```bash
docker compose -f docker-compose.bridge.yaml up text-encoder -d   # 等到 "healthy"
docker compose -f docker-compose.bridge.yaml ps

# Live 推論（預載模型 —— log 出現 "Application startup complete" 才就緒）
MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
curl http://localhost:8001/health     # {"status":"ok","mock_mode":false}
```

> **Mock 模式**（`MOCK_MODE=1`,預設）跳過推論、回傳 `output/dev_reference.npz`
> —— 適合無 GPU 開發 HDA。
>
> **更改埠號：** API 預設 **8001**(Windows 上 8000 被 Docker Desktop 佔用)。
> 若 8001 已被占用,啟動 api 時設 `KIMODO_PORT`,並把節點的 **API Server URL** 指到同一埠:
> ```bash
> KIMODO_PORT=8002 MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
> # 然後在 HDA:API Server URL = http://localhost:8002
> ```

## 6. Houdini Python 套件

```bash
# Linux / macOS
$HFS/bin/hython -m pip install requests scipy numpy
# Windows（請依你的 Houdini 路徑調整 HFS）
"C:\Program Files\Side Effects Software\Houdini 21.0.xxx\bin\hython.exe" -m pip install requests scipy numpy
```

## 7. 安裝與使用 HDA

repo 已附上建好的 HDA 於 `hda/`,在 Houdini 安裝即可
(**Assets → Install Asset Library…** 或用 package 檔;見 [hda/README.md](../hda/README.md))。

在 SOP 網路放一個 **`kimodo_motion`** 節點:

1. 把 **API Server URL** 設成 `http://localhost:8001`(或 GPU 主機)。
2. 設 **Prompt** 與 **Duration**,按 **Generate**。NPZ 會下載到 **Download Dir**
   (`$HIP/kimodo_cache`),節點自動 cook。
3. 節點有四個輸出 —— **Animated Pose**、**Capture Pose**、**Rest Geometry**(蒙皮身體 mesh)、**T-Pose**。
4. 要變形身體:加一個 **`kinefx::jointdeform`**,接 input 0 = Rest Geometry、
   input 1 = Capture Pose、input 2 = Animated Pose。

> **Constraints(選用):** 想引導動作命中空間目標,可提供 [Kimodo constraints](https://research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/constraints.html)
> JSON(**Constraints File** / **Constraints JSON**)、把曲線/點接到 input 0 授權 root 路徑,
> 或用 **Create Pose Rig** 擺骨架(input 1)授權 full-body / end-effector 約束
> —— 見 [hda/README.zh-TW.md](../hda/README.zh-TW.md#constraints選用)。
>
> 若修改了 cook 腳本,可重建 HDA:
> `hython scripts/build_skin.py` → `hython scripts/create_hda.py` →
> `hython scripts/_add_help.py` —— 見 [hda/README.zh-TW.md](../hda/README.zh-TW.md#重建-hda)。
