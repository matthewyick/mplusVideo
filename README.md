# mplusVideo 2025

工業級 IP 攝影機錄影與監控系統（1～6 路高畫質 RTSP，支援 Axis 及任何 RTSP 品牌）。

系統採用「後端多行程精靈（Daemons）+ 作業系統核心共享記憶體（Shared Memory）+ PyQt6 前端監控台」架構，
前端關閉不中斷錄影、自動斷線重連、錄影零 CPU 壓縮損耗（H.264 直拷貝）。

---

## 1. 系統組成

| 檔案 | 角色 | 說明 |
|------|------|------|
| `mplusVideo_Recorder_2025.py` | 後端錄影（Daemon） | RTSP 拉流、異常偵測、寫入 MP4、寫入共享記憶體；CMD 輸入 `QUIT` 停止。 |
| `mplusVideo_Recorder_Console.py` | 前端監控台（PyQt6） | 從共享記憶體讀取即時畫面與狀態，動態網格排列 1～6 路；關閉不影響錄影。 |
| `config.json` | 啟動器設定 | 只放 `root_folder`（可選）。實際攝影機設定在 `[root_folder]\\config\\config.json`。 |

架構圖：

```
 IP 攝影機層 (Axis / Dahua / 任何 RTSP)
        │ RTSP (TCP)
        ▼
 後端錄影 Daemon (每路一個 Thread) ──┬──► FFmpeg ──► MP4 (H.264 video [+ AAC audio]) ──► 本地硬碟
        │                            │
        │ (寫入 Raw RGB Matrix)       │ (OpenCV 顯示用擷取)
        ▼                            ▼
 ┌──────────────────────┐    ┌──────────────────────┐
 │ axis_stream_shm_XX   │    │ (畫面凍結/斷線偵測)   │
 │ axis_stream_meta_XX  │    └──────────────────────┘
 └──────────────────────┘
        ▲ Zero-copy 映射讀取 (33ms)
        │
 前端 PyQt6 監控台（可隨時關閉/重開，不影響錄影）
```

---

## 2. 環境需求

### 2.1 Python 套件

| 套件 | 版本建議 | 用途 |
|------|----------|------|
| `opencv-python` | 4.8+（實測 4.10） | RTSP 擷取、BGR→RGB、凍結偵測 |
| `numpy` | 1.24+（實測 2.2） | Raw 影像矩陣、共享記憶體視圖 |
| `PyQt6` | 6.5+（實測 6.11） | 前端監控台（僅前端需要） |

安裝：

```bash
pip install opencv-python numpy PyQt6
```

> 僅執行後端錄影時，**不需要**安裝 PyQt6。

---

## 3. 音訊錄影與 FFmpeg 需求（本次更新重點）

### 3.1 更新摘要

- 舊版以 `cv2.VideoWriter`（mp4v）錄影，**無法封裝音訊**，產出的 MP4 只有影像。
- 新版改由 **FFmpeg 直接拉取 RTSP 串流**寫入 MP4：
  - 影像：`-c:v copy` —— **H.264 直拷貝、零轉檔 CPU、零畫質損失**。
  - 音訊：`-c:a aac -b:a 64k` —— 若攝影機有音軌，一併封裝為 AAC。
- **每路攝影機自動偵測是否有音訊**（以 `ffprobe` 判斷），因此：
  - 支援多品牌混合（部分攝影機有音訊、部分沒有），互不影響。
  - 有音訊 → 錄成 `video + audio`；無音訊 → 錄成 `video only`，皆不報錯。

### 3.2 FFmpeg / FFprobe 需求（必裝）

本程式**必須**在系統 PATH 中可執行以下兩支命令：

| 命令 | 用途 | 必要性 |
|------|------|--------|
| `ffmpeg` | 實際錄影（拉流 + H.264 直拷貝 + AAC 封裝 + MP4 分段） | **錄影必需** |
| `ffprobe` | 啟動時偵測每路 RTSP 是否含音軌 | 建議（缺少時自動以 video-only 錄影，不中斷） |

**版本需求**：FFmpeg 4.0 以上（建議 6.0+）。需支援 `aac` 編碼與 `-movflags +faststart`（標準建置皆內建）。

驗證是否已安裝：

```bat
ffmpeg -version
ffprobe -version
```

若未安裝：

- **Windows**：下載 https://www.gyan.dev/ffmpeg/builds/ ，解壓後將 `bin` 目錄加入系統 PATH。
- **以 winget 安裝**：`winget install Gyan.FFmpeg`
- 安裝後**重新開啟 CMD / 終端機**，再執行 `ffmpeg -version` 確認。

### 3.3 自訂 FFmpeg 路徑（可選）

若 FFmpeg 不在 PATH，或想指定特定版本，可透過環境變數指定完整路徑：

```bat
set MPLUSVIDEO_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
set MPLUSVIDEO_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
```

程式內對應常數：

| 常數 | 預設值 | 環境變數 |
|------|--------|----------|
| `FFMPEG_BIN` | `ffmpeg` | `MPLUSVIDEO_FFMPEG` |
| `FFPROBE_BIN` | `ffprobe` | `MPLUSVIDEO_FFPROBE` |
| `AUDIO_BITRATE` | `64k` | — |

> IP 攝影機音訊多為 8kHz 單聲道 A-law（G.711），`64k` AAC 已足夠；若需更高音質可調整 `AUDIO_BITRATE`。

### 3.4 錄影實際使用的 FFmpeg 指令（參考）

每路攝影機、每個分段會執行類似以下指令（`has_audio=True` 時額外加入音訊 map）：

```
ffmpeg -y -loglevel error -rtsp_transport tcp -i "<rtsp_url>" ^
  -map 0:v:0 -c:v copy ^
  -map 0:a:0? -c:a aac -b:a 64k ^
  -t 300 -movflags +faststart "<output.mp4>"
```

- `-rtsp_transport tcp`：強制 TCP，避免 UDP 封包遺失（綠屏/破碎）。
- `-map 0:a:0?`：`?` 表示音訊可選 —— 攝影機沒有音軌時不會導致失敗。
- `-t 300`：每個分段 300 秒後自我結束並完成 finalize（寫入 moov atom）。
- 停止錄影（`QUIT`）時，程式以 **CTRL_BREAK_EVENT** 優雅停止 FFmpeg，確保 MP4 完整可播放。

> 重要：程式刻意**不使用** `CREATE_NO_WINDOW` 啟動 FFmpeg（會導致 CTRL_BREAK 無法送達、MP4 毀損），改用 `CREATE_NEW_PROCESS_GROUP` + `STARTUPINFO(SW_HIDE)` 隱藏視窗。

---

## 4. 設定檔

### 4.1 啟動器 `config.json`（與程式同資料夾，可選）

```json
{ "root_folder": "E:\\root_mplusVideo2025" }
```

### 4.2 攝影機設定 `[root_folder]\config\config.json`

```json
{
  "server": { "ip_address": "127.0.0.1", "port": 80 },
  "ip_cameras": [
    {
      "cam_id": 1,
      "cam_name": "Camera 1",
      "rtsp_url": "rtsp://root:password@192.168.1.50/axis-media/media.amp",
      "width": 1920, "height": 1080,
      "brand": "AXIS", "model": "1349", "compression": "265"
    },
    {
      "cam_id": 2,
      "cam_name": "Camera 2",
      "rtsp_url": "rtsp://admin:password@192.168.1.80/cam/realmonitor?channel=1&subtype=0",
      "width": 1920, "height": 1080,
      "brand": "Dahua", "model": "1349", "compression": "265"
    }
  ],
  "output_directory": "E:\\root_mplusVideo2025"
}
```

設定解析順序：

1. 讀同資料夾 `config.json` 的 `root_folder`（或環境變數 `MPLUSVIDEO_ROOT`）。
2. 載入 `[root_folder]\config\config.json` 內的 `ip_cameras` 與 `output_directory`。
3. 若 `output_directory` 不存在則自動建立，並產生今日三層資料夾。

> 注意：JSON 內 Windows 路徑的反斜線請用 `\\`（或 `/`）。程式對未轉義路徑亦有自動修復機制，但建議仍使用正確轉義。

---

## 5. 輸出檔案

- 目錄結構（今日錄影皆存於最末層 `DD` 資料夾）：

```
output_directory\
  └ 2026\
       └ 11\
            └ 07\        ← 今日錄影存放處
                 ├ 01_20261107_093505.mp4
                 └ 02_20261107_093506.mp4
```

- 檔名格式：`[cam_id (二位補零)]_YYYYMMDD_HHMMSS.mp4`
- 每個分段 300 秒（`SEGMENT_SECONDS`），自動輪替並乾淨 finalize。
- 有音訊的攝影機 → MP4 內含 H.264 影像 + AAC 音訊；無音訊 → 僅 H.264 影像。

---

## 6. 使用方式

### 6.1 後端錄影

```bat
python mplusVideo_Recorder_2025.py
```

啟動後於 CMD 顯示各攝影機狀態與音訊偵測結果，例如：

```
[Camera 1] AUDIO: OFF (video only)
[Camera 2] AUDIO: ON (video+audio)
[Camera 1] REC 開始 (video) -> ...\01_20261107_093505.mp4
[Camera 2] REC 開始 (video+audio) -> ...\02_20261107_093506.mp4
```

停止錄影：在 CMD 輸入 `QUIT` 後按 Enter。程式會優雅停止所有 FFmpeg，確保 MP4 完整。

### 6.2 前端監控台（PyQt6）

```bat
python mplusVideo_Recorder_Console.py
```

- 自動尋找後端已建立的共享記憶體並動態排列網格（1～6 路）。
- 後端尚未啟動時每 3 秒重試，不會閃退。
- 以 Zero-copy（記憶體指針建 QImage）+ FastTransformation 縮放，大幅降低 UI CPU。
- **關閉監控視窗只釋放前端連線，絕不 unlink 共享記憶體，後端錄影不中斷。**

---

## 7. 共享記憶體介面（前後端契約）

| 名稱 | 型別 | 內容 |
|------|------|------|
| `axis_stream_shm_XX` | RGB24 Raw Frame | `height * width * 3` bytes |
| `axis_stream_meta_XX` | `uint64[3]` | `[0]`=影格序號、`[1]`=狀態碼、`[2]`=更新時間(unix) |

狀態碼：`1 = STATUS_OK`、`0 = STATUS_WARNING`（斷線/凍結警告畫面會以黑底紅字寫入）。

---

## 8. 異常處理與日誌

- 日誌目錄：`[root_folder]\logs\`
- 日誌檔案：
  - `[root_folder]\logs\YYYY-MM-DD.log`：一般執行日誌（含 `[START TIME]` / `[END TIME]`）。
  - `[root_folder]\logs\error-YYYY-MM-DD.log`：錯誤日誌（含時間戳、錯誤訊息、Stack Trace）。
- 一律以附加（Append）方式寫入，不覆寫既有內容。
- RTSP 帳號密碼於日誌/CMD 中一律遮蔽為 `rtsp://***:***@...`。
- 異常偵測：
  - 解碼失敗連續達門檻 → 重置。
  - 畫面凍結超過 5 秒 → 於共享記憶體顯示黑底紅字警告，並自動重新開流。
  - 斷線自動重連（指數退避，2s～30s），連線穩定 30 秒後重置等待時間。

---

## 9. 參數調整（程式頂端常數）

| 常數 | 預設 | 說明 |
|------|------|------|
| `MAX_FPS` | 25 | 顯示用最高幀率，降低 CPU |
| `FREEZE_THRESHOLD_SEC` | 5.0 | 畫面凍結判定秒數 |
| `SEGMENT_SECONDS` | 300 | 每個 MP4 分段長度（秒） |
| `AUDIO_BITRATE` | `64k` | AAC 音訊位元率 |
| `MAX_CAMERAS` | 6 | 支援路數上限 |
| `RECONNECT_BASE_WAIT` / `RECONNECT_MAX_WAIT` | 2.0 / 30.0 | 重連等待（指數退避） |

---

## 10. 疑難排解

| 症狀 | 可能原因 | 解法 |
|------|----------|------|
| 找不到 ffmpeg，無法錄影 | 未安裝或不在 PATH | 安裝 FFmpeg 並加入 PATH，或設定 `MPLUSVIDEO_FFMPEG` |
| MP4 有影像沒聲音 | 該攝影機無音軌，或 ffprobe 偵測失敗 | 以 `ffprobe "<rtsp_url>"` 確認是否含 `audio` stream |
| 錄影檔無法播放 / moov 遺失 | 上次被強制 kill（非正常停止） | 以 `QUIT` 正常停止；確認使用新版（CTRL_BREAK 優雅停止） |
| 影像綠屏/破碎 | 使用 UDP 傳輸 | 系統已強制 TCP；確認攝影機允許 TCP RTSP |
| 前端顯示「等待後端共享記憶體」 | 後端未啟動或路數不符 | 先啟動後端；確認 `config.json` 路數一致 |

