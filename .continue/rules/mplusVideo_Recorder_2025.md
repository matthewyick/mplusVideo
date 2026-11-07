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
* ** 讀取 "ip_cameras" 內所有 ip cameras 的資料。
* ** 讀取 "output_directory" 內的資料夾，如果資料夾不存在，請產生該資料夾。並根據今日日子產生三層資料夾。
* 例如 7 November 2026, 產生 
* root_mplusVideo
  └ 2026
       └ 11
          └ 07
* 今日錄影的影片都儲存在這 07 資料夾 

### 2.1 日期與基礎陣列初始化
