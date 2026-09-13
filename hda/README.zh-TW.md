# HDA 函式庫（繁體中文說明）

> **Note (English):** this translation describes the upstream v1.0 interface of chordee/kimodo-houdini-bridge. The fxhoudinikimodo changes (output order, tabs, frame-based duration, retiming, Windows notes) are documented in the English files only.

[English](README.md)

本目錄收錄 Kimodo-Houdini 橋接專案的 Houdini Digital Asset，以**解包（unpacked）格式**
儲存——副檔名為 `.hda/` 的目錄,內容為純文字,可在 git 中 diff 與 review。

---

## kimodo_motion.hda

**節點類型：** `Sop/kimodo_motion`  
**使用環境：** SOP（幾何網路）  
**最低 Houdini 版本：** H20.5

一個 SOP 節點,透過 [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) 模型,把自然語言
prompt 轉成 3D 人體動作。它把 prompt 送到執行中的 `kimodo_server`、透過 HTTP 下載輸出的
NPZ,並重建 77 關節 SOMA 動作為 KineFX 相容幾何——動畫骨架、rest 骨架、以及蒙皮身體 mesh。

### 輸出

四個輸出。世界空間旋轉採 Houdini 的 row-vector / row-major 慣例(已從 Kimodo 的
column-vector 轉置)。骨架以 polyline 連接父子關節。

| # | 標籤 | 內容 |
|---|------|------|
| 0 | **Animated Pose** | 逐幀動畫骨架。`name`、`path`、`parent_id`、`transform`(float[9] 世界旋轉)、`localtransform`(float[16] 局部 4×4);NPZ 帶有足部接觸資料時另有 `contact`(int 0/1),見 [足部接觸](#足部接觸)。 |
| 1 | **Capture Pose** | mesh 綁定的 A-pose rest 骨架(腳在地面)。`name`、`transform`。 |
| 2 | **Rest Geometry** | SOMA77 身體 mesh 的 bind 姿勢,帶 KineFX `boneCapture` 屬性(權重 + bind 來自 Kimodo 蒙皮)。 |
| 3 | **T-Pose** | T-pose 骨架(`name`、`transform`),供參考 / retarget。 |

**要變形身體**:放一個 **`kinefx::jointdeform`**(Labs/KineFX Joint Deform),接:
input 0 = **Rest Geometry**(輸出 2)、input 1 = **Capture Pose**(輸出 1)、
input 2 = **Animated Pose**(輸出 0)。mesh 會跟著動畫變形,rest 時回到 bind。
(輸出 0 也可直接接 Rig Pose / Bone Deform 流程。)

### 輸入（選用）

節點有兩個**選用輸入**,都是用幾何授權約束、不必寫 JSON(見 [Constraints](#constraints選用));
一般使用時不接即可:

- **Input 0 — Root Path / Waypoints**:一條曲線或一組點 → `root2d` 約束。
- **Input 1 — Pose Keyframes (skeleton)**:一副擺好的 SOMA77 骨架 → full-body 或
  end-effector 約束(見 [Pose 約束](#pose-約束input-1))。

### 參數說明

| 參數 | 預設 | 說明 |
|------|------|------|
| API Server URL | `http://localhost:8001` | 執行中的 `kimodo_server` 網址。伺服器在別台就指過去。 |
| Download Dir | `$HIP/kimodo_cache` | 從伺服器透過 HTTP 下載 NPZ 的本機資料夾。 |
| Prompt | `a person walks forward` | 動作的自然語言描述。越具體(部位、方向、速度、風格)越好。 |
| Duration (s) | `3.0` | 片段長度(秒)。30 fps 下 3 秒 = 90 幀。 |
| Model | `Kimodo-SOMA-RP-v1.1` | Kimodo 模型變體。`RP` 以 rest pose 為條件;`SEED` 固定種子可重現。 |
| Force Regenerate | 關閉 | 繞過伺服器快取,即使有相同片段也強制重算。 |
| Constraints File | _(空)_ | 選用的 [Kimodo constraints](https://research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/constraints.html) JSON 檔(`*.json`)。見 [Constraints](#constraints選用)。 |
| Constraints JSON | _(空)_ | 選用的內嵌 constraints JSON;非空時優先於 Constraints File。 |
| Pose Constraint | `Full-Body` | input 1 的擺好骨架要怎麼用:`Full-Body`(整副)或 `End-Effector`。見 [Pose 約束](#pose-約束input-1)。 |
| Left/Right Hand/Foot | Right Hand | `End-Effector` 時:要釘住哪些關節(其餘身體保持自由)。 |
| **Create Pose Rig** | — | 在網路裡丟一個獨立 A-pose rig(+ Rig Pose)並接到 input 1,可直接擺。 |
| Pose Keyframes | _(空)_ | 要取樣 input 1 的幀,例如 `0 45 89`。空 = 無 pose 約束。 |
| **Generate** | — | 送 prompt 到 `<API Server URL>/generate` 並立即返回 job ID。背景執行緒輪詢進度,Houdini 不凍結。完成後 NPZ 下載到 **Download Dir**、設定 **NPZ Path**、節點重新 cook。 |
| **Cancel** | — | 取消工作。常駐伺服器無法中斷執行中的生成——Cancel 只停掉排隊中的工作或丟棄結果。 |
| Status | _(唯讀)_ | 即時狀態:`Queued`、`Running... (N秒)`、`Downloading...`、`Done (N秒)`、`Failed`、`Cancelled`。 |
| NPZ Path | _(空)_ | 節點用來建骨架的 `.npz`——是**檔案欄位**(可瀏覽,`*.npz`)。由 Generate 設定;也可手動指向任何相容 NPZ(不需 Generate/伺服器)。空 = 輸出空幾何直到設定。 |

#### NPZ 檔案是什麼？

NPZ(NumPy 壓縮封存)是 Kimodo 的推論輸出。節點讀取:

| 鍵值 | 形狀 | 內容 |
|------|------|------|
| `posed_joints` | `(T, 77, 3)` | 各關節世界座標位置(公尺)—— **節點會讀取**(關節位置) |
| `global_rot_mats` | `(T, 77, 3, 3)` | 各關節世界空間旋轉 —— **節點會讀取**;`transform`/`localtransform` 由此推導 |
| `local_rot_mats` | `(T, 77, 3, 3)` | 各關節局部旋轉(Kimodo 輸出;節點不需要) |
| `root_positions` | `(T, 3)` | 根節點(Hips)世界位置 |
| `foot_contacts` | `(T, 6)` | 足部接觸標記(布林)—— 存在時**節點會讀取**,轉為輸出 0 的 `contact` |

節點重建骨架只需 **`posed_joints`** 與 **`global_rot_mats`**(SOMA77 關節順序)。任何相容的
NPZ 都能用,不管怎麼產生的——把 **NPZ Path** 指過去即可。**Download Dir** 只在 Generate 時用到。

#### 足部接觸

NPZ 帶有 `foot_contacts` 時,輸出 0 會多一個 `int` 點屬性 **`contact`**:該幀腳關節著地為
`1`,其餘皆為 `0`(包含所有非腳部關節)。沒有這個鍵值的 NPZ 就不會有 `contact` 屬性——請
檢查屬性是否存在,不要把「值為 0」直接當成「從未著地」。

這些標記來自模型本身:Kimodo 預測一個接觸通道並以 0.5 為閾值二值化,並非在此由關節運動反
推。Kimodo 內部也會用它在輸出動作前修正腳滑。

接觸偵測只在**每側兩個關節**上進行——腳踝與腳趾根部。SOMA77 的 NPZ 會有六個通道,其中
`LeftToeEnd` / `RightToeEnd` 是對應 `ToeBase` 通道的複製,並非獨立偵測(四通道的 NPZ ——
Kimodo 內部表示,未經 SOMA77 展開——同樣讀得進來,見下方說明):

| 通道 | 關節 | |
|---|---|---|
| 0 | `LeftFoot` | 獨立偵測 |
| 1 | `LeftToeBase` | 獨立偵測 |
| 2 | `LeftToeEnd` | 複製自通道 1 |
| 3 | `RightFoot` | 獨立偵測 |
| 4 | `RightToeBase` | 獨立偵測 |
| 5 | `RightToeEnd` | 複製自通道 4 |

兩種情況下所有關節都會寫入屬性——是寫在全部 77 個點上,不只腳部——所以要驅動 foot lock
時,請把兩個 `ToeEnd` 視為冗餘資訊。若是**四通道**的 NPZ,兩個 `ToeEnd` 沒有任何通道對應,
整段動畫都會是 `0`,與「從未著地」無法區分;此時請改用 `ToeBase`。

常見用途:在 `contact == 1` 期間鎖住腳部以消除腳滑,或偵測 `0` → `1` 的轉換作為腳步事件,
用來驅動塵土、貼花或音效。

### Constraints（選用）

[Kimodo constraints](https://research.nvidia.com/labs/sil/projects/kimodo/docs/key_concepts/constraints.html)
可引導生成的動作命中空間目標:root 2D 路徑或 waypoint、full-body keyframe、或
end-effector(手/腳)目標。提供一個 JSON **constraint dict 列表**,每個 dict 的 `type` 欄位
決定約束種類。

**Constraints File 與 Constraints JSON 不是約束種類**,而是提供**同一份** JSON 的兩種來源:
把 **Constraints File** 指向 `*.json`(例如 Kimodo demo 匯出的),或把 JSON 貼進
**Constraints JSON**。內嵌 JSON 非空時優先,否則讀檔案。一個列表可同時含多種類型的約束。
兩者都留空即為無約束生成。(若有接輸入幾何,會再加一個 `root2d` —— 見下方。)

目標採 Kimodo 座標系:**Y-up、公尺、+Z 朝前、root 於 frame 0 在 XZ = (0, 0)**——與本節點
輸出的世界空間相同。最單純的是 `root2d` waypoint(`frame_indices` + `[x, z]` 配對):

```json
[{"type": "root2d", "frame_indices": [0, 90], "smooth_root_2d": [[0, 0], [2, 1]]}]
```

其他類型(`fullbody`、`left-hand`/`right-hand`/`left-foot`/`right-foot`)另需各關節旋轉;
建議在 Kimodo demo 內授權後匯出 JSON。constraints 也納入快取鍵,新的 constraint 會觸發重新生成。

**用幾何授權 `root2d`(input 0):** 不想寫 JSON 時,可把幾何接到節點的選用輸入。每個點的
世界 XZ 會變成一個 `smooth_root_2d` 目標(Houdini XZ 與 Kimodo 空間 1:1 對應 —— 可直接描節點
自己輸出的軌跡)。點上若帶整數 `frame` 屬性 → 變成那些幀的**稀疏 waypoint**(由你控制時機);
否則點依序(例如重取樣後的 polyline)均勻分布到整段片段,當成**較密的路徑**。由幾何產生的
`root2d` 會接在上面的 JSON constraints 之後。

#### Pose 約束（input 1）

用擺 pose 的方式授權 **full-body** 或 **end-effector** 約束,不必手寫逐關節 JSON:

1. 按 **Create Pose Rig**。它會在網路裡丟一個**獨立的 A-pose rig**(一個 `kimodo_pose_rig`
   來源 + 一個 KineFX **Rig Pose**),並自動接到節點的 **input 1** —— 完全不從本節點的輸出
   牽線,所以沒有迴圈。
2. 在那個 Rig Pose 上擺姿勢 / 打 keyframe,定義想釘住的 pose。
3. 在 **Pose Keyframes** 填要取樣的幀(例如 `0 45 89`),並選 **Pose Constraint** =
   Full-Body 或 End-Effector(後者再勾要釘的 Hand/Foot,其餘身體保持自由)。

每個 keyframe 會讀擺好骨架各關節的世界位置與旋轉,送到 server,由它直接建
`FullBodyConstraintSet` / `EndEffectorConstraintSet`(與 Kimodo demo 相同路徑)。原理是把
節點自身的前向變換反推回去,所以 rig **必須是 Create Pose Rig 產生的那一副**(它載入同一副
A-pose、帶著預期的旋轉慣例);任意 rig 無法正確轉換。

### 快取

伺服器以 `prompt + duration + model + constraints` 的 SHA-256 雜湊為鍵快取。相同設定再跑會
即時回傳(Status 顯示 `Done (cached)`);開 **Force Regenerate** 可繞過。

### 前置條件

一個執行中的 `kimodo_server`。在 kimodo 目錄(見 [安裝指南](../docs/setup.zh-TW.md)):

```bash
docker compose -f docker-compose.bridge.yaml up text-encoder -d   # 等到 healthy
MOCK_MODE=0 docker compose -f docker-compose.bridge.yaml up api -d
```

模型在啟動時從本機 HuggingFace 快取離線載入,所以權重要先快取
(`huggingface-cli download nvidia/Kimodo-SOMA-RP-v1.1`)。之後模型常駐 VRAM,直到停掉 api 容器。

---

## 重建 HDA

若修改了 cook 腳本或蒙皮,重新生成 HDA:

```bash
# 1. 建置內嵌的 skin 幾何(mesh + capture、A-pose 骨架)
hython scripts/build_skin.py

# 2. 在 repo 根重建 packed HDA(嵌入 skin sections)
hython scripts/create_hda.py

# 3. 加入 help card 並存成解包格式至 hda/
hython scripts/_add_help.py
```

---

## 安裝方式

### 方式 A — Houdini Package（推薦）

把 package 檔複製到 Houdini packages 目錄,再把 `KIMODO_BRIDGE_ROOT` 改成此 repo 的絕對路徑:

```bash
# Windows
copy fxhoudinikimodo.json %HOUDINI_USER_PREF_DIR%\packages\
# Linux / macOS
cp fxhoudinikimodo.json ~/houdiniXX.Y/packages/
```

重啟 Houdini,即可在 Tab 選單的 **Kimodo** 分類下找到 `kimodo_motion` 節點。

### 方式 B — 手動安裝

Houdini:**Assets → Install Asset Library…** → 選 `hda/kimodo_motion.hda/`。
