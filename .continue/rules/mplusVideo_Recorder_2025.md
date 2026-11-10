# 開發需求說明書：後端錄影 (`mplusVideo_Recorder_2025.py`)

你是一個資深的 python 開發專家。請根據以下規格，為專案開發一個名為 `mplusVideo_Recorder_2025.py` 的程式。


## 1. 執行環境與開發規範（硬性約束）

* **檔案名稱**：`mplusVideo_Recorder_2025.py`
* **執行模式**：在 CLI 執行 python mplusVideo_Recorder_2025.py 指令
* **架構限制（極重要）**：
  * 必須嚴格遵循專案既有的 `@Specification.md` 規格。
  * **禁止使用全域變數** (`global`)。
  * **禁止使用相依性注入** (Dependency Injection)。

## 2. 核心功能與資料邏輯
* 負責 RTSP 拉流、異常偵測、寫入 MP4，並將最新的 Raw 影像矩陣高速寫入作業系統的共享記憶體（Shared Memory）。
* 讀取在同一資料夾的 config.json
* ** 讀取 `root_folder`，
* ** 用 `[root_folder]` + `\config\config.json` 的資料夾位置。
* ** `[root_folder]` + `\config\config.json`
* ** 讀取 "ip_cameras" 內所有 ip cameras 的資料。
* ** 讀取 "output_directory" 內的資料夾，如果資料夾不存在，請產生該資料夾。並根據今日日子產生三層資料夾。
* 例如 7 November 2026, 產生 
* root_mplusVideo
  └ 2026
       └ 11
          └ 07
* 今日錄影的影片都儲存在這 07 資料夾 
* mp4 的檔案名稱是： [cam_id (with leading zero)]_YYYYMMDD_HHIISS.mp4
* 每一個 rtsp_url 必須獨立用一個 thread 來錄影。
* 能夠處理 1 到 6 路高畫質 IP 攝影機（包括 Axis 或任何支援 RTSP 的品牌），且具前端監控台關閉不中斷錄影、自動斷線重連、零 CPU 壓縮損耗的工業級系統，採用架構是 「後端多行程精靈（Multi-processing Daemons） + 核心共享記憶體（Shared Memory）。
* PyQt6 前端監控台」能從核心共享記憶體（Shared Memory）讀取實時錄影進度和狀態。


### 2.1 初始化共享記憶體（作業系統層級的記憶體區塊）

### 2.2 強制 OpenCV 使用 TCP 傳輸協定，防止 UDP 封包遺失導致畫面綠屏或破碎

### 2.3【異常偵測機制 A】：解碼失敗，或【機制 B】：畫面凍結超過 5 秒

### 2.4 在共享記憶體中填滿黑色，並印上紅色警告字樣，直接通知前端 UI

### 2.5 釋放資源並嘗試重新開流

### 2.6 重置重連等待時間

### 2.7 高速色彩空間轉換 (OpenCV 的 BGR 轉為 PyQt 偏好的 RGB)

### 2.8 利用 np.copyto 進行記憶體直寫（零拷貝覆寫，不重複分配 RAM 空間）

### 2.9 限制最高約 25 FPS，避免過度消耗 CPU

### 2.10 在 CMD 等待用戶指令，輸入QUIT停止錄影。

