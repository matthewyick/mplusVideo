# 開發需求說明書：後端錄影 (`mplusVideo_Recorder_Console.py`)

你是一個資深的 python 開發專家。請根據以下規格，為專案開發一個名為 `mplusVideo_Recorder_Console.py` 的程式。


## 1. 執行環境與開發規範（硬性約束）

* **檔案名稱**：`mplusVideo_Recorder_Console.py`
* **執行模式**：在 CLI 執行 python mplusVideo_Recorder_Console.py 指令
* **架構限制（極重要）**：
  * 必須嚴格遵循專案既有的 `@Specification.md` 規格。
  * 必須參考 `mplusVideo_Recorder_2025.py` 如何寫入 Raw 影像矩陣高速寫入作業系統的共享記憶體（Shared Memory）。
  * **禁止使用全域變數** (`global`)。
  * **禁止使用相依性注入** (Dependency Injection)。
* 這個前端程式會自動尋找作業系統中存在的攝影機共享記憶體，並動態排列成一個乾淨的網格介面。
* 使用 PyQT6 作為介面。

## 2. 核心功能與資料邏輯
* 清除所有舊有的共享記憶體。
* 首先檢查需要用的軟件
* ** 是否能夠呼叫 ffmpeg 和 ffprobe 。如果沒有，請用視窗通知用戶需要安裝 ffmpeg, ffprobe 或下載 ffmpeg, ffprobe 並在系統環境的PATH 加上 ffmpeg, ffprobe 路徑。
* ** 檢查和 `mplusVideo_Recorder_Console.py` 的相同路徑 config.json 是否存在。如果沒有展出對白匣，請用戶輸入本地路徑以作為儲存影片，日誌之用。
* ** 檢查是否能產生剛輸入的路徑。如果不成功顯示錯誤信息請求再輸入或退出。
* ** 如果成功產生剛輸入的路徑，在該路徑下產生 config, logs 兩個資料夾。
在`mplusVideo_Recorder_Console.py` 的相同路徑產生 config.json ，內容是 "root_folder" 和它的值，值是剛輸入的路徑。
* ** 如果 ("`root_folder`" + "\config") 的路徑沒有 config.json，展出視窗請求用戶輸入一到六個 RTSP URL 輸入匣，最少輸入一個 RTSP URL，和正確的 RTSP URL 格式。請確定防止注射攻擊。
* **  "cam_id" 值是輸入匣的 index。
* **  "cam_name" 值是 "camera" + "cam_id"
* **  "width": 1920
* **  "height": 1080,
* **  "brand": "AXIS",
* **  "model": "1349",
* **  "compression":"265"
* **  然後儲存 config.json 到 ("`root_folder`" + "\config")。
* **  如果 ("`root_folder`" + "\config") 的路徑有 config.json 讀取內容。

* 然後啟動介面，根據的("`root_folder`" + "\config\config.json") RTSP URL 讀取的串流。
* 介面上有一個 "Start Recording" 切換按鈕。
* 點擊後，用 cmd 啟動在同一路徑的 python 程式 mplusVideo_Recorder_2025.py。按鈕上的敘述改成 "Stop Recording"。
* 點擊 "Stop Recording"按鈕，停止 mplusVideo_Recorder_2025.py 程式。
* 然後 摘要按鈕 展出這次錄影的日誌和錯誤信息。


* 讀取在同一資料夾的 config.json
* ** 讀取 "ip_cameras" 內所有 ip cameras 的資料。
* ** 讀取 "output_directory" 內的資料夾，如果資料夾不存在，請產生該資料夾。並根據今日日子產生三層資料夾。
* 例如 7 November 2026, 產生 
* root_mplusVideo
  └ 2026
       └ 11
          └ 07
* 今日錄影的影片都儲存在這 07 資料夾 
* mp4 的檔案名稱是： [cam_id (with leading zero)]_YYYYMMDD_HHIISS.mp4
* 根據 rstp_url 的數量展示 1 到 6 路高畫質 IP 攝影機。
* 使用網格佈局來呈現多路畫面
* 從核心共享記憶體（Shared Memory）讀取實時錄影進度和狀態。

## 2.1 啟動記憶體對接嘗試
## 2.2 嘗試掛載已經存在的作業系統共享記憶體
## 2.3 將該記憶體區塊映射為結構化的 Numpy 陣列視圖
## 2.4 如果後端還沒開，3秒後再次嘗試，確保前端不會閃退
## 2.5 【工業級核心優化】：直接利用記憶體指針建立 QImage，完全無複製動作（Zero-copy）
## 2.6 使用 FastTransformation（鄰近點插值法）進行多路影像縮放，節省 80% 的 UI CPU 開銷
## 2.7 監控視窗手動關閉。正在釋放前端記憶體連接
## 2.8 關閉本 UI 行程的連線，【絕不刪除 unlink】記憶體，保護後端錄影！
