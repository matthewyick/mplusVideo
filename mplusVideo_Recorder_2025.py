"""
mplusVideo_Recorder_2025.py
===========================

工業級 IP 攝影機錄影後端（Backend Recorder Daemon）。

架構：
    * 主行程 (Main Process)  : 讀取 config.json、建立共享記憶體、啟動錄影執行緒、等待 QUIT。
    * 錄影執行緒 (Thread)     : 每一個 rtsp_url 一個獨立 Thread（RTSP 拉流 / MP4 寫入 / 重連）。
    * 共享記憶體 (SharedMem)  : 每個攝影機一塊作業系統層級的 Shared Memory 區塊，
                                內容為「最新的 Raw RGB 影像矩陣」。
    * 硬碟儲存                : mp4 片段（segment）放在 output_directory/YYYY/MM/DD/ 下。

使用方式：
    python mplusVideo_Recorder_2025.py

    CMD 輸入 QUIT 可停止所有錄影並安全結束程式。

硬性規範：
    * 禁止使用 global 關鍵字。
    * 禁止使用相依性注入 (Dependency Injection)，所有設定一律以建構子 / 參數傳遞。
    * 日誌寫入 logs/YYYY-MM-DD.log 與 logs/error-YYYY-MM-DD.log（附加模式）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing import shared_memory
from multiprocessing.shared_memory import SharedMemory
from typing import Optional

import cv2
import numpy as np

# =============================================================================
# 常數（模組層級唯讀設定，非可變全域狀態）
# =============================================================================
APP_NAME = "mplusVideo_Recorder_2025"

CONFIG_DIR_NAME = "config"
CONFIG_FILE_NAME = "config.json"
ROOT_FOLDER_ENV = "MPLUSVIDEO_ROOT"

SHM_PREFIX = "axis_stream_shm"
SHM_META_PREFIX = "axis_stream_meta"

# 共享記憶體資料佈局（與前端 mplusVideo_Recorder_Console.py 必須一致）：
#   主區塊  : RGB24 Raw Frame (height * width * 3)
#   中介區塊: uint64[0]=影格序號 / uint64[1]=狀態碼 / uint64[2]=更新時間(unix)
META_CELLS = 3
STATUS_OK = 1
STATUS_WARNING = 0

# 錄影 / 效能參數
MAX_FPS = 25                    # 限制最高約 25 FPS，避免過度消耗 CPU
FREEZE_THRESHOLD_SEC = 5.0      # 畫面凍結超過 5 秒視為異常
DECODE_FAIL_LIMIT = 5           # 連續解碼失敗次數上限
READ_TIMEOUT_MSEC = 5000        # OpenCV 讀取逾時（毫秒）
OPEN_TIMEOUT_MSEC = 5000        # OpenCV 開啟逾時（毫秒）
RECONNECT_BASE_WAIT = 2.0       # 重連等待初始值（秒）
RECONNECT_MAX_WAIT = 30.0       # 重連等待上限（秒）
RECONNECT_RESET_AFTER_SEC = 30.0  # 串流穩定超過此秒數後，重置重連等待時間

# 影片分段（每個 mp4 片段長度，避免長時錄影檔損毀）
SEGMENT_SECONDS = 300

# FFmpeg recording (H.264 video copy + AAC audio mux)
FFMPEG_BIN = os.environ.get("MPLUSVIDEO_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("MPLUSVIDEO_FFPROBE", "ffprobe")
AUDIO_BITRATE = "64k"            # IP cameras are usually 8kHz mono A-law; 64k is enough
FFMPEG_PROBE_TIMEOUT_SEC = 10    # ffprobe audio detection timeout
FFMPEG_STOP_GRACE_SEC = 6        # graceful stop grace period for ffmpeg

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MAX_CAMERAS = 6


# =============================================================================
# 日誌機制
# =============================================================================
class DailyLogger:
    """每日日誌（YYYY-MM-DD.log 一般日誌、error-YYYY-MM-DD.log 錯誤日誌）。"""

    def __init__(self, log_folder: str, app_name: str = APP_NAME) -> None:
        self.log_folder = os.path.abspath(log_folder)
        self.app_name = app_name
        self._lock = threading.RLock()
        self._make_dir(self.log_folder)

    @staticmethod
    def _make_dir(path: str) -> None:
        try:
            if path and not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
        except Exception:  # noqa: BLE001 - 日誌機制本身絕不可讓程式崩潰
            pass

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _append(self, filename: str, text: str) -> None:
        """附加寫入；檔案不存在則自動建立，絕不覆寫既有內容。"""
        self._make_dir(self.log_folder)
        full_path = os.path.join(self.log_folder, filename)
        with self._lock:
            try:
                if not os.path.exists(full_path):
                    with open(full_path, "a", encoding="utf-8") as fp:
                        fp.write("")
                with open(full_path, "a", encoding="utf-8") as fp:
                    fp.write(text + "\n")
            except Exception:  # noqa: BLE001
                pass

    def log(self, level: str, message: str, cam_tag: str = "",
            exc: Optional[BaseException] = None) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = f"[{cam_tag}] " if cam_tag else ""
        line = f"{stamp} [{level}] {tag}{message}"
        if exc is not None:
            line += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._append(f"{self._today()}.log", line)
        if level in ("ERROR", "CRITICAL"):
            self.error(message, cam_tag=cam_tag, exc=exc)

    def debug(self, message: str, cam_tag: str = "") -> None:
        self.log("DEBUG", message, cam_tag)

    def info(self, message: str, cam_tag: str = "") -> None:
        self.log("INFO", message, cam_tag)

    def warning(self, message: str, cam_tag: str = "") -> None:
        self.log("WARNING", message, cam_tag)

    def error(self, message: str, cam_tag: str = "", exc: Optional[BaseException] = None) -> None:
        now = datetime.now()
        prefix = f"[{cam_tag}] " if cam_tag else ""
        body = f"=== [{now.strftime('%H:%M:%S')}] [{self.app_name}] ERROR {prefix}{message}"
        if exc is not None:
            body += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._append(f"error-{self._today()}.log", body + "\n")

    def start_banner(self) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log("INFO", f"[START TIME] {stamp} - 同步程序啟動")

    def end_banner(self) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log("INFO", f"[END TIME] {stamp} - 同步程序順利結束")


def mask_url(url: str) -> str:
    """遮蔽 RTSP URL 中的帳號密碼，避免憑證出現在 CMD / 日誌中。"""
    if not url:
        return url
    try:
        head, _, rest = url.partition("://")
        if not rest:
            return url
        authority, sep, tail = rest.partition("/")
        if "@" in authority:
            authority = "***:***@" + authority.rsplit("@", 1)[1]
        return f"{head}://{authority}{sep}{tail}"
    except Exception:  # noqa: BLE001
        return "***"


def debug_print(message: str, cam_tag: str = "") -> None:
    """在 CMD 展出除錯信息。"""
    stamp = datetime.now().strftime("%H:%M:%S")
    tag = f"[{cam_tag}] " if cam_tag else ""
    print(f"[{stamp}] {tag}{message}", flush=True)


# =============================================================================
# 設定資料結構
# =============================================================================
@dataclass(frozen=True)
class CameraConfig:
    """單一攝影機設定。"""

    cam_id: int
    cam_name: str
    rtsp_url: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    brand: str = "GENERIC"
    model: str = ""
    compression: str = ""

    @property
    def zero_padded_id(self) -> str:
        return str(self.cam_id).zfill(2)

    @property
    def shm_name(self) -> str:
        return f"{SHM_PREFIX}_{self.zero_padded_id}"

    @property
    def shm_meta_name(self) -> str:
        return f"{SHM_META_PREFIX}_{self.zero_padded_id}"

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 3  # RGB24


@dataclass(frozen=True)
class RecorderConfig:
    """整體設定（config.json 內容 + 衍生路徑）。"""

    cameras: list = field(default_factory=list)
    output_directory: str = ""
    ip_address: str = "127.0.0.1"
    port: int = 80
    log_folder: str = ""
    error_log_folder: str = ""
    today_dir: str = ""
    root_folder: str = ""
    config_path: str = ""

    @property
    def camera_count(self) -> int:
        return len(self.cameras)


# =============================================================================
# 設定檔定位：[root_folder] + \config\config.json
# =============================================================================
def resolve_root_folder(script_dir: str, argv: Optional[list] = None) -> str:
    """決定 root_folder。

    優先順序：
        1. 命令列參數 : python mplusVideo_Recorder_2025.py --root-folder D:\\root_mplusVideo
        2. 環境變數   : MPLUSVIDEO_ROOT
        3. 程式所在資料夾的上一層（若上一層含 config/config.json）
        4. 程式所在資料夾（向下相容單層部署）
    """
    argv = argv or []
    for index, token in enumerate(argv):
        if token == "--root-folder" and index + 1 < len(argv):
            return os.path.abspath(argv[index + 1])
        if token.startswith("--root-folder="):
            return os.path.abspath(token.split("=", 1)[1])

    env_root = os.environ.get(ROOT_FOLDER_ENV)
    if env_root and env_root.strip():
        return os.path.abspath(env_root.strip())

    parent = os.path.dirname(os.path.abspath(script_dir))
    if os.path.isfile(os.path.join(parent, CONFIG_DIR_NAME, CONFIG_FILE_NAME)):
        return parent
    return os.path.abspath(script_dir)


def _repair_windows_path(value: str) -> str:
    """修復 JSON 中未轉義的反斜線造成的路徑損壞（例如 E:\\root 被讀成 E:<CR>oot）。"""
    if not value:
        return value
    # JSON 未轉義 \r \n \t \b \f 會變成控制字元，這裡還原為反斜線 + 原字母
    repaired = (
        value.replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    repaired = "".join(ch if ord(ch) >= 32 else "\\" for ch in repaired)
    # 優先使用原始路徑，破損時才用修復後的路徑
    return value if os.path.isdir(value) else repaired


def read_root_folder(config_path: str) -> Optional[str]:
    """從啟動用的 config.json 讀取 root_folder 欄位（若存在）。

    會自動修復 Windows 路徑中被 JSON 吃掉的反斜線（例如 "E:\\root_..."）。
    """
    try:
        with open(config_path, "r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None

    value = data.get("root_folder")
    if not value or not str(value).strip():
        return None

    candidate = str(value).strip()
    for path in (candidate, _repair_windows_path(candidate)):
        if os.path.isdir(path):
            return os.path.abspath(path)
    # 皆不存在：回傳修復後的絕對路徑，交由後續流程建立 / 報錯
    return os.path.abspath(_repair_windows_path(candidate))


def locate_config_file(root_folder: str, script_dir: str = "") -> Optional[str]:
    """定位攝影機設定檔。

    優先順序（兩階段設定）：
        1. [root_folder]\\config\\config.json              <- 主要設定（攝影機清單）
        2. [script_dir]\\config\\config.json               <- 部署在同一資料夾
        3. [script_dir]\\config.json                       <- 向下相容單層部署
    """
    candidates = [os.path.join(root_folder, CONFIG_DIR_NAME, CONFIG_FILE_NAME)]
    if script_dir:
        candidates.append(os.path.join(script_dir, CONFIG_DIR_NAME, CONFIG_FILE_NAME))
        candidates.append(os.path.join(script_dir, CONFIG_FILE_NAME))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _parse_camera(raw: dict, index: int) -> CameraConfig:
    """將 config.json 的一筆 ip_cameras 轉為 CameraConfig。"""
    cam_id = raw.get("cam_id", index + 1)
    try:
        cam_id = int(cam_id)
    except (TypeError, ValueError):
        cam_id = index + 1

    width = int(raw.get("width", DEFAULT_WIDTH) or DEFAULT_WIDTH)
    height = int(raw.get("height", DEFAULT_HEIGHT) or DEFAULT_HEIGHT)
    if width <= 0:
        width = DEFAULT_WIDTH
    if height <= 0:
        height = DEFAULT_HEIGHT

    return CameraConfig(
        cam_id=cam_id,
        cam_name=str(raw.get("cam_name", f"Camera {cam_id}")),
        rtsp_url=str(raw.get("rtsp_url", "")),
        width=width,
        height=height,
        brand=str(raw.get("brand", "GENERIC")),
        model=str(raw.get("model", "")),
        compression=str(raw.get("compression", "")),
    )


def build_output_directory(root: str, when: Optional[datetime] = None) -> str:
    """建立 root/YYYY/MM/DD 三層資料夾，回傳 DD 資料夾路徑。"""
    when = when or datetime.now()
    target = os.path.join(root, when.strftime("%Y"), when.strftime("%m"), when.strftime("%d"))
    os.makedirs(target, exist_ok=True)
    return target


# ----------------------------------------------------------------------------- #
# FFmpeg 程序旗標（Windows 專用，用於可送到 CTRL_BREAK 的獨立程序群組）
# ----------------------------------------------------------------------------- #
def _ffmpeg_creation_flags() -> int:
    """Windows 下讓 ffmpeg 進入獨立程序群組（可接收 CTRL_BREAK_EVENT）。

    注意：不可使用 CREATE_NO_WINDOW！該旗標會讓子程序失去主控台，
    導致 CTRL_BREAK_EVENT 無法送達，ffmpeg 卡死後被強制 kill，
    MP4 就會缺少 moov atom 而毀損。改用 STARTUPINFO 隱藏視窗（見 _ffmpeg_startupinfo）。
    """
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _ffmpeg_startupinfo():
    """回傳隱藏主控台視窗的 STARTUPINFO（不影響 CTRL_BREAK 的送達）。"""
    if os.name != "nt":
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        si.wShowWindow = 0  # SW_HIDE
        return si
    except Exception:  # noqa: BLE001
        return None


def _send_graceful_stop(proc: subprocess.Popen) -> None:
    """對 ffmpeg 送出 CTRL_BREAK_EVENT（等同互動式按 Ctrl-Break），讓它收尾 finalize。

    非 Windows 或送信號失敗時，退回 proc.terminate()。
    """
    import signal as _signal  # 本地匯入，避免污染模組頂層
    try:
        if os.name == "nt" and hasattr(_signal, "CTRL_BREAK_EVENT"):
            proc.send_signal(_signal.CTRL_BREAK_EVENT)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- #
# 音訊偵測：用 ffprobe 判斷 RTSP 串流是否含音軌（每路攝影機可能不同）
# ----------------------------------------------------------------------------- #
def detect_audio_stream(url: str, logger: DailyLogger, cam_name: str = "") -> bool:
    """回傳 RTSP 串流是否含有聲稱的音訊軌。

    使用 ffprobe 逐條列出 stream，若出現 codec_type=audio 即視為有音訊。
    偵測失敗（工具不存在 / 逾時 / 連線失敗）一律回傳 False，確保錄影不中斷。
    """
    if not url:
        return False
    probe_exe = FFPROBE_BIN
    if shutil.which(probe_exe) is None and shutil.which(FFPROBE_BIN) is None:
        logger.warning("找不到 ffprobe，將以無音訊方式錄影", cam_name)
        return False

    cmd = [
        probe_exe,
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "a",          # 只查音訊串流
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "default=noprint_wrappers=1",
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_PROBE_TIMEOUT_SEC,
        )
        text = (result.stdout or b"").decode("utf-8", "ignore").lower()
        has_audio = "codec_type=audio" in text
        logger.debug(f"音訊偵測: {'有音訊' if has_audio else '無音訊'} ({mask_url(url)})", cam_name)
        return has_audio
    except subprocess.TimeoutExpired:
        logger.warning("音訊偵測逾時，將以無音訊方式錄影", cam_name)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("音訊偵測失敗，將以無音訊方式錄影", cam_name, exc)
        return False


def load_recorder_config(root_folder: str, logger: DailyLogger,
                         config_path: Optional[str] = None) -> RecorderConfig:
    """讀取 [root_folder]\\config\\config.json，並建立輸出資料夾。

    不使用全域變數、不使用相依性注入：所有參數皆由呼叫端明確傳入。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if not config_path:
        config_path = locate_config_file(root_folder, script_dir)
    if not config_path or not os.path.isfile(config_path):
        raise FileNotFoundError(
            f"找不到設定檔，請確認存在: {os.path.join(root_folder, CONFIG_DIR_NAME, CONFIG_FILE_NAME)}"
        )

    # 若傳入的 root_folder 之下沒有 config/config.json，但實際讀到的設定檔位於
    # [real_root]\\config\\config.json，則以實際設定檔的父層作為 root_folder。
    real_root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    if not os.path.normcase(real_root).startswith(os.path.normcase(os.path.abspath(root_folder))):
        root_folder = real_root
    root_folder = os.path.abspath(root_folder)

    logger.debug(f"root_folder : {root_folder}")
    logger.debug(f"讀取設定檔: {config_path}")
    with open(config_path, "r", encoding="utf-8-sig") as fp:
        raw = json.load(fp)

    server = raw.get("server", {}) or {}
    ip_address = str(server.get("ip_address", "127.0.0.1"))
    try:
        port = int(server.get("port", 80))
    except (TypeError, ValueError):
        port = 80

    cameras_raw = raw.get("ip_cameras", []) or []
    cameras = [_parse_camera(item, idx) for idx, item in enumerate(cameras_raw)]
    if not cameras:
        raise ValueError("config.json 內 ip_cameras 為空，無攝影機可錄影。")
    if len(cameras) > MAX_CAMERAS:
        logger.warning(f"ip_cameras 超過 {MAX_CAMERAS} 路，僅取前 {MAX_CAMERAS} 路。")
        cameras = cameras[:MAX_CAMERAS]

    output_directory = str(raw.get("output_directory", "")).strip()
    if not output_directory:
        raise ValueError("config.json 未設定 output_directory。")
    # 修復 JSON 未轉義反斜線造成的損壞（例如 "E:\\root_..." 被讀成 "E:<CR>oot_..."）
    output_directory = _repair_windows_path(output_directory)
    output_directory = os.path.normpath(output_directory)

    log_folder = os.path.join(root_folder, "logs")

    os.makedirs(output_directory, exist_ok=True)
    today_dir = build_output_directory(output_directory)
    logger.debug(f"輸出目錄已就緒: {today_dir}")
    logger.debug(f"讀取到 {len(cameras)} 路攝影機設定")

    return RecorderConfig(
        cameras=cameras,
        output_directory=output_directory,
        ip_address=ip_address,
        port=port,
        log_folder=log_folder,
        error_log_folder=log_folder,
        today_dir=today_dir,
        root_folder=root_folder,
        config_path=config_path,
    )


# =============================================================================
# 錯誤畫面產生（斷線 / 凍結 警告）
# =============================================================================
def build_warning_frame(width: int, height: int, lines: list) -> np.ndarray:
    """建立黑底 + 紅色警告字樣的 RGB 影格（支援中文，失敗時退回英文）。"""
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    try:
        _draw_warning_text(canvas, lines)
    except Exception:  # noqa: BLE001 - 沒有 PIL / 中文字型時退回 OpenCV 英文
        cv2.putText(
            canvas,
            "!! SIGNAL LOST !!",
            (max(10, int(width * 0.05)), max(40, height // 2)),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.8, width / 1200.0),
            (255, 0, 0),
            max(2, int(width / 600)),
            cv2.LINE_AA,
        )
    return canvas


def _draw_warning_text(canvas: np.ndarray, lines: list) -> None:
    """以 PIL + 中文字型繪製；若無 PIL 則拋出例外給呼叫端。"""
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    height, width = canvas.shape[:2]
    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)

    font = None
    for font_path in (
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msjhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ):
        try:
            if os.path.exists(font_path):
                font = ImageFont.truetype(font_path, max(16, int(height / 22)))
                break
        except Exception:  # noqa: BLE001
            continue
    if font is None:
        font = ImageFont.load_default()

    line_height = max(22, int(height / 16))
    y = max(10, (height - len(lines) * line_height) // 2)
    for text in lines:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
        except Exception:  # noqa: BLE001
            text_width = len(text) * line_height // 2
        draw.text(((width - text_width) // 2, y), text, font=font, fill=(255, 32, 32))
        y += line_height

    canvas[:, :, :] = np.asarray(image)


# =============================================================================
# 共享記憶體寫入器
# =============================================================================
class SharedFrameWriter:
    """管理單一攝影機的共享記憶體區塊（Raw RGB Frame + 影格序號 + 狀態碼）。"""

    def __init__(self, camera: CameraConfig, logger: DailyLogger) -> None:
        self.camera = camera
        self.logger = logger
        self._shm: Optional[shared_memory.SharedMemory] = None
        self._meta: Optional[shared_memory.SharedMemory] = None
        self._frame_view: Optional[np.ndarray] = None
        self._rgb_buffer: Optional[np.ndarray] = None
        self._meta_view: Optional[np.ndarray] = None

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        """初始化共享記憶體（作業系統層級的記憶體區塊）。"""
        self._create_or_attach(self.camera.shm_name, self.camera.frame_bytes, "_shm")
        self._create_or_attach(self.camera.shm_meta_name, META_CELLS * 8, "_meta")

        if self._shm is None or self._meta is None:
            raise RuntimeError(f"{self.camera.cam_name}: 共享記憶體初始化失敗")

        # 將該記憶體區塊映射為結構化的 Numpy 陣列視圖（無拷貝）
        self._frame_view = np.ndarray(
            (self.camera.height, self.camera.width, 3),
            dtype=np.uint8,
            buffer=self._shm.buf,
            offset=0,
        )
        # 預先配置一個 RGB 緩衝區，僅在首次配置，之後用 np.copyto 直寫
        self._rgb_buffer = np.empty((self.camera.height, self.camera.width, 3), dtype=np.uint8)
        self._meta_view = np.ndarray((META_CELLS,), dtype=np.uint64, buffer=self._meta.buf, offset=0)
        # 重用舊段時清除舊的狀態計數，避免前端顯示舊的狀態
        self._meta_view[:] = 0

        self.paint_warning(["啟動中...", "Connecting..."])
        self.logger.debug(
            f"共享記憶體就緒: {self.camera.shm_name} ({self.camera.frame_bytes} bytes, "
            f"{self.camera.width}x{self.camera.height} RGB)",
            self.camera.cam_name,
        )
        debug_print(
            f"SHM {self.camera.shm_name} ready ({self.camera.width}x{self.camera.height})",
            self.camera.cam_name,
        )

    def _create_or_attach(self, name: str, size: int, attr: str) -> None:
        """建立或重用共享記憶體。

        工業級修正（WinError 183 嚰死）：
        若上次程式崩潰留下殘留的同名區段，Windows 下 unlink() 必須在
        所有 handle 都釋放後才真正生效；單次 unlink+重建常會失敗。
        因此采用：先嘗試直接連接（若尺寸相符則直接重用），否則循環多次
        unlink + 重建，並加入短延遲讓 Windows 收回名稱。最後仍失敗才拑出。
        """
        # 1) 嘗試直接建立（正常情況）
        try:
            shm = shared_memory.SharedMemory(name=name, create=True, size=size)
            setattr(self, attr, shm)
            self.logger.debug(f"建立共享記憶體 {name} size={size}", self.camera.cam_name)
            return
        except FileExistsError:
            pass

        # 2) 已存在：若尺寸相符則直接重用（避免後端重啟時碰到前次殘留）
        try:
            existing = shared_memory.SharedMemory(name=name, create=False)
            if existing.size >= size:
                setattr(self, attr, existing)
                self.logger.debug(f"重用已存在共享記憶體 {name} size={size}", self.camera.cam_name)
                return
            existing.close()
        except Exception:  # noqa: BLE001
            pass

        # 3) 尺寸不符或無法連接 -> 循環清除殘留並重建
        last_exc = None
        for attempt in range(1, 8):
            try:
                stale = shared_memory.SharedMemory(name=name, create=False)
                try:
                    stale.close()
                finally:
                    try:
                        stale.unlink()
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            try:
                shm = shared_memory.SharedMemory(name=name, create=True, size=size)
                setattr(self, attr, shm)
                self.logger.warning(f"清除殘留並重建共享記憶體 {name}", self.camera.cam_name)
                return
            except FileExistsError as exc:
                last_exc = exc
                time.sleep(0.1 * attempt)
        # 4) 仍失敗才拁出讓上層記錄
        raise RuntimeError(f"無法建立共享記憶體 {name} size={size}") from last_exc

    # ------------------------------------------------------------------ #
    def write_frame(self, rgb: np.ndarray) -> None:
        """利用 np.copyto 進行記憶體直寫（零配置覆寫，不重複分配 RAM）。"""
        if self._frame_view is None or self._rgb_buffer is None or self._meta_view is None:
            return
        h, w = self._frame_view.shape[:2]
        if rgb.shape[0] != h or rgb.shape[1] != w:
            rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_NEAREST)
        # 先寫入預配置緩衝，再直寫共享記憶體（避免前端讀到半張影格）
        np.copyto(self._rgb_buffer, rgb)
        np.copyto(self._frame_view, self._rgb_buffer)
        self._bump_counter(STATUS_OK)

    def paint_warning(self, lines: list) -> None:
        """在共享記憶體中填滿黑色，並印上紅色警告字樣，直接通知前端 UI。"""
        if self._frame_view is None:
            return
        try:
            frame = build_warning_frame(self.camera.width, self.camera.height, lines)
            self._frame_view[:] = frame
            self._bump_counter(STATUS_WARNING)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("繪製警告畫面失敗", self.camera.cam_name, exc)

    def _bump_counter(self, status: int) -> None:
        """更新影格序號 / 狀態碼 / 更新時間（讓前端知道有新影格）。"""
        if self._meta_view is None:
            return
        self._meta_view[0] += 1
        self._meta_view[1] = np.uint64(status)
        self._meta_view[2] = np.uint64(int(time.time()))

    # ------------------------------------------------------------------ #
    def close(self, unlink: bool = True) -> None:
        """釋放共享記憶體（後端結束時才 unlink，前端不處理）。"""
        for attr in ("_shm", "_meta"):
            shm = getattr(self, attr, None)
            if shm is None:
                continue
            try:
                shm.close()
            except Exception:  # noqa: BLE001
                pass
            if unlink:
                try:
                    shm.unlink()
                    self.logger.debug(f"已釋放共享記憶體 {shm.name}", self.camera.cam_name)
                except FileNotFoundError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(f"釋放共享記憶體失敗 {shm.name}", self.camera.cam_name, exc)
            setattr(self, attr, None)
        self._frame_view = None
        self._rgb_buffer = None
        self._meta_view = None


# =============================================================================
# 錄影寫入（FFmpeg：H.264 直拷貝 + AAC 音訊）
# =============================================================================
class SegmentWriter:
    """每一路攝影機的分段 MP4 錄影器（FFmpeg 後端）。

    設計要點：
        * 由 FFmpeg 直接拉取 RTSP 串流（video: copy 直拷貝，零轉檔 CPU）。
        * 攝影機若有音軌 -> 一併轉成 AAC 寫入同一個 MP4（每路自動偵測）。
        * 每個分段以 -t <秒數> 自我結束，Python 端僅負責輪替與重啟，
          因此每段都能乾淨 finalize（寫入 moov atom），不會產生毀損檔。
        * 顯示用的 OpenCV 擷取與共享記憶體管線完全不變，互不干擾。
    """

    def __init__(self, camera: CameraConfig, output_root: str, fps: int,
                 logger: DailyLogger, has_audio: bool = False) -> None:
        self.camera = camera
        self.output_root = output_root
        self.fps = fps
        self.logger = logger
        self.has_audio = bool(has_audio)
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[str] = None
        self._opened_at: float = 0.0
        self._day_key: str = ""
        self._frames_written: int = 0
        self._last_bytes: int = 0

    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """啟動第一段錄影（由外部呼叫，或第一次 write 時自動啟動）。"""
        if self._proc is None:
            self._open(time.time(), datetime.now().strftime("%Y%m%d"))

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def update(self, frame_bgr: Optional[np.ndarray] = None) -> None:
        """維持錄影：檢查段長 / 跨日 / FFmpeg 存活並在需要時輪替。

        frame_bgr 僅用於估算畫面更新（不再送進 FFmpeg，錄影由 FFmpeg 自行拉流）。
        """
        now = time.time()
        day_key = datetime.now().strftime("%Y%m%d")

        if self._proc is None:
            self._open(now, day_key)
            return

        # FFmpeg 中途結束（時間到 / 串流斷）-> 重新啟動下一段
        if self._proc.poll() is not None:
            self._finalize_current(reason="ffmpeg exited")
            self._open(now, day_key)
            return

        # 跨日或段長到期 -> 輪替（FFmpeg 以 -t 已自我結束，這裡確保即時接手）
        if day_key != self._day_key or (now - self._opened_at) >= SEGMENT_SECONDS:
            self.close_current(reason="rotate")
            self._open(now, day_key)
            return

        self._frames_written += 1
        # 每 25 幀刷新一次檔案大小（僅用於回報，不做磁碟負擔）
        if self._frames_written % 25 == 0 and self._path:
            try:
                self._last_bytes = os.path.getsize(self._path)
            except OSError:
                pass

    # 保留舊介面：write() 等同 update()，維持與既有呼叫端相容
    def write(self, frame_bgr: Optional[np.ndarray] = None) -> None:
        self.update(frame_bgr)

    def _segment_path(self, now_dt: datetime) -> str:
        """[cam_id (with leading zero)]_YYYYMMDD_HHMMSS.mp4"""
        folder = build_output_directory(self.output_root, now_dt)
        filename = f"{self.camera.zero_padded_id}_{now_dt.strftime('%Y%m%d_%H%M%S')}.mp4"
        return os.path.join(folder, filename)

    def _build_command(self, path: str) -> list:
        """組出 FFmpeg 指令：video copy + (可選) audio AAC + 分段限時。"""
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.camera.rtsp_url,
            "-map", "0:v:0",
            "-c:v", "copy",
        ]
        if self.has_audio:
            cmd += [
                "-map", "0:a:0?",
                "-c:a", "aac",
                "-b:a", AUDIO_BITRATE,
            ]
        cmd += [
            "-t", str(SEGMENT_SECONDS),
            "-movflags", "+faststart",
            path,
        ]
        return cmd

    def _open(self, now: float, day_key: str) -> None:
        self._finalize_current(reason="open")
        path = self._segment_path(datetime.now())
        cmd = self._build_command(path)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                creationflags=_ffmpeg_creation_flags(),
                startupinfo=_ffmpeg_startupinfo(),
            )
        except FileNotFoundError as exc:
            self.logger.error(f"找不到 ffmpeg 執行檔: {FFMPEG_BIN}", self.camera.cam_name, exc)
            debug_print(f"[ERROR] 找不到 ffmpeg，無法錄影: {FFMPEG_BIN}", self.camera.cam_name)
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.error("啟動 FFmpeg 失敗", self.camera.cam_name, exc)
            return

        self._proc = proc
        self._path = path
        self._opened_at = now
        self._day_key = day_key
        self._frames_written = 0
        self._last_bytes = 0
        audio_note = "video+audio" if self.has_audio else "video"
        self.logger.info(f"開始錄影檔案: {path} ({audio_note})", self.camera.cam_name)
        debug_print(f"REC 開始 ({audio_note}) -> {path}", self.camera.cam_name)

    def _finalize_current(self, reason: str = "") -> None:
        """結束目前 FFmpeg 程序並等待 MP4 finalize。"""
        proc = self._proc
        path = self._path
        if proc is not None:
            try:
                if proc.poll() is None:
                    # 先給 FFmpeg 自然結束（-t 到期）一點時間
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                                                # Graceful stop: CTRL_BREAK lets ffmpeg finalize the moov atom,
                        # producing a playable MP4. Falls back to terminate/kill.
                        _send_graceful_stop(proc)
                        try:
                            proc.wait(timeout=FFMPEG_STOP_GRACE_SEC)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            # 讀掉 stderr，避免 pipe 堆積並記錄錯誤
            try:
                err = (proc.stderr.read() or b"").decode("utf-8", "ignore").strip() if proc.stderr else ""
                if err:
                    self.logger.warning(f"ffmpeg: {err[-500:]}", self.camera.cam_name)
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

        if path:
            size = 0
            try:
                size = os.path.getsize(path) if os.path.isfile(path) else 0
            except OSError:
                size = 0
            self.logger.info(
                f"結束錄影檔案: {path} ({size} bytes, reason={reason})",
                self.camera.cam_name,
            )
            debug_print(f"REC end <- {path} ({size} bytes, {reason})", self.camera.cam_name)

        self._proc = None
        self._path = None
        self._frames_written = 0

    def close_current(self, reason: str = "") -> None:
        self._finalize_current(reason=reason)




# =============================================================================
# 單一攝影機錄影執行緒
# =============================================================================
class CameraRecorderThread(threading.Thread):
    """每一個 rtsp_url 一個獨立 Thread 負責 RTSP 拉流、錄影與共享記憶體更新。

    完全獨立：某一路斷線只會影響該執行緒自己的重連迴圈，
    其他攝影機（執行緒）完全不受影響。
    """

    def __init__(self, camera: CameraConfig, output_root: str, logger: DailyLogger,
                 has_audio: bool = False) -> None:
        super().__init__(name=f"Recorder-{camera.zero_padded_id}", daemon=True)
        self.camera = camera
        self.output_root = output_root
        self.logger = logger
        self.has_audio = bool(has_audio)
        self._stop_event = threading.Event()
        self._shm_writer = SharedFrameWriter(camera, logger)
        self._segment = SegmentWriter(camera, output_root, MAX_FPS, logger, has_audio=self.has_audio)
        self._reconnect_wait = RECONNECT_BASE_WAIT
        # 此 OpenCV 建置是否接受逾時參數開法（失敗就不再重試，避免重連時刷屏）
        self._timeout_params_supported = True
        self._last_checksum: Optional[int] = None
        self._last_change_ts = time.time()
        # 真實狀態字串（給 StatusMonitor 顯示用，非執行緒存活與否）
        self._state = "INIT"

    # ------------------------------------------------------------------ #
    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    @property
    def state(self) -> str:
        """真實狀態：CONNECTING / RECORDING / OFFLINE / WAIT / STOPPED / INIT。"""
        return self._state

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        tag = self.camera.cam_name
        try:
            self._shm_writer.open()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("共享記憶體初始化失敗，執行緒終止", tag, exc)
            return

        # 每路攝影機的音訊能力不同：開始錄影前先偵測一次。
        self._segment.has_audio = detect_audio_stream(self.camera.rtsp_url, self.logger, tag)
        debug_print(f"AUDIO: {'ON (video+audio)' if self._segment.has_audio else 'OFF (video only)'}", tag)

        # 工業級防護：單一次錄影會話失敗不得終止整個後端執行緒。
        # 只要使用者尚未 QUIT，就重新安全重連，直到成功或停止。
        while not self.stopping:
            try:
                self._record_session()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("錄影會話異常，將重新安全重連", tag, exc)
                debug_print(f"[WARN] 錄影會話異常，重連中: {exc}", tag)
                try:
                    self._segment.close_current(reason="session error")
                except Exception:  # noqa: BLE001
                    pass
                if not self.stopping:
                    self._sleep_reconnect()

        # 只在明確停止（QUIT）時才清理資源。
        try:
            self._segment.close_current(reason="thread exit")
        finally:
            self._state = "STOPPED"
            self._shm_writer.close(unlink=True)
            self.logger.info("錄影執行緒已停止", tag)
            debug_print("錄影執行緒已停止", tag)

    # ------------------------------------------------------------------ #
    def _build_ffmpeg_options(self) -> str:
        """回傳 FFMPEG 選項字串（強制 TCP，必要時可調整）。

        mplus 2025 使用 OpenCV 的 CAP_PROP_* 逾時參數，並在 URL 上附加
        transport=tcp；此方法用於環境變數後備方案。
        """
        # stimeout: FFmpeg socket timeout (microseconds). This build of OpenCV
        # rejects the CAP_PROP_*_TIMEOUT_MSEC params ("unsupported parameters"),
        # so we enforce the socket timeout via this env option instead. It makes
        # cap.read() return False on a dead/silent socket rather than blocking
        # forever, WITHOUT needing a second reader thread (which previously
        # caused the fctx->async_lock assertion / abort crash on disconnect).
        stimeout_us = max(1, int(READ_TIMEOUT_MSEC)) * 1000
        return f"rtsp_transport;tcp|stimeout;{stimeout_us}|max_delay;500000"

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        """開流 RTSP。

        工業級修正之二：本機的 OpenCV 4.10 對於 CAP_PROP_*_TIMEOUT_MSEC 參數會
        回報 "unsupported parameters ... Bailout"，即證它不接受這些參數，也就代表
        cap.read() 對已斷線的 socket 可能無限阻塞。因此優先使用環境變數
        OPENCV_FFMPEG_CAPTURE_OPTIONS（帶 rtsp_transport;tcp 與 stimeout）來開流，
        由 FFmpeg 自身在 socket 逾時後讓 read() 回傳 False，沒有第二個讀取執行緒，
        因此不會再觸發 fctx->async_lock 的 abort 籍死。
        若環境變數方式失敗，才退回舊有的參數 / URL transport 方式。
        """
        tag = self.camera.cam_name
        url = self.camera.rtsp_url

        # 策略 1（優先）：環境變數帶 stimeout 開流
        cap = self._open_with_env_tcp(url)
        if cap is not None:
            self.logger.debug(f"開流成功（env+stimeout, {mask_url(url)}）", tag)
            return cap

        # 策略 2（回退）：URL transport=tcp + CAP_PROP 參數
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MSEC,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MSEC,
            cv2.CAP_PROP_BUFFERSIZE, 1,
        ]
        attempt_urls = [url]
        if url.lower().startswith("rtsp://") and "transport=" not in url.lower():
            sep = "&" if "?" in url else "?"
            attempt_urls.insert(0, f"{url}{sep}transport=tcp")

        for candidate in attempt_urls:
            param_options = (params, None) if self._timeout_params_supported else (None,)
            for open_params in param_options:
                cap = None
                try:
                    cap = (cv2.VideoCapture(candidate, cv2.CAP_FFMPEG, open_params)
                           if open_params else cv2.VideoCapture(candidate, cv2.CAP_FFMPEG))
                    if cap.isOpened():
                        try:
                            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        except Exception:  # noqa: BLE001
                            pass
                        self.logger.debug(
                            f"開流成功（回退, {mask_url(candidate)}), timeout_params={bool(open_params)}",
                            tag,
                        )
                        return cap
                    cap.release()
                except Exception as exc:  # noqa: BLE001
                    self.logger.debug(f"回退開流失敗 ({mask_url(candidate)}): {exc}", tag)
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:  # noqa: BLE001
                            pass

            if self._timeout_params_supported and len(param_options) > 1:
                self._timeout_params_supported = False
                self.logger.debug("偵測到 OpenCV 不支援 timeout 參數，後續改用無參數開流", tag)
        return None

    def _open_with_env_tcp(self, url: str) -> Optional[cv2.VideoCapture]:
        """後備方案：設定 OPENCV_FFMPEG_CAPTURE_OPTIONS 強制 TCP。"""
        tag = self.camera.cam_name
        previous = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        self.logger.debug("嘗試後備方案：環境變數強制 TCP", tag)
        try:
            if url.lower().startswith("rtsp://"):
                opts = previous or ""
                if "rtsp_transport" not in opts:
                    opts = (opts + "|" if opts else "") + self._build_ffmpeg_options()
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                return cap
            cap.release()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("後備開啟串流失敗", tag, exc)
        finally:
            if previous is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = previous
        return None

    # ------------------------------------------------------------------ #
    def _timed_read(self, cap: cv2.VideoCapture, timeout: float = READ_TIMEOUT_MSEC / 1000.0):
        """單執行緒、每輪僅一次讀取影格，回傳 (ok, frame)。

        工業級穩定性修正：舊版每呼叫一次就開一個 daemon 執行緒跑 cap.read()，
        逾時後 join() 返回但背景執行緒仍存活並持續呼叫同一個 cap.read()，
        下一輪又開新執行緒，造成多執行緒同時對同一個 VideoCapture 呼叫 read()。
        拔除網路線時便觸發 OpenCV 內嵌 libavcodec 的
            Assertion fctx->async_lock failed at libavcodec/pthread_frame.c:173
        導致 abort() 使整個 Python 行程籍死。

        現改為只依賴 OpenCV 內建的 CAP_PROP_READ_TIMEOUT_MSEC（已於開流時設定），
        cap.read() 斷線時會自行逾時回傳 False，不再有跨執行緒同時存取同一個 cap，
        從根本消除上述 abort 籍死。
        """
        start = time.time()
        try:
            ok, frame = cap.read()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"cap.read() 例外: {exc}", self.camera.cam_name)
            return False, None
        elapsed = time.time() - start
        if elapsed > timeout:
            self.logger.debug(
                f"cap.read() 耗時 {elapsed:.1f}s（> {timeout:.1f}s），視為可疑",
                self.camera.cam_name,
            )
        return ok, frame

    def _record_session(self) -> None:
        """單次連線的錄影循環；結束後由外層進行重連。"""
        tag = self.camera.cam_name
        self._shm_writer.paint_warning(["連線中...", f"Connecting {self.camera.cam_name}"])
        debug_print(f"連線 RTSP: {mask_url(self.camera.rtsp_url)}", tag)

        self._state = "CONNECTING"
        cap = self._open_capture()
        if cap is None:
            self._state = "OFFLINE"
            self.logger.warning("無法開啟 RTSP 串流，進入重連等待", tag)
            self._shm_writer.paint_warning(["無法連線攝影機", "Camera Offline", self.camera.cam_name])
            self._sleep_reconnect()
            return

        self.logger.info(f"RTSP 串流已連線: {mask_url(self.camera.rtsp_url)}", tag)
        debug_print("RTSP 串流已連線", tag)

        self._state = "RECORDING"
        session_start = time.time()
        last_frame_ts = time.time()
        failed_reads = 0
        frame_interval = 1.0 / float(MAX_FPS)
        self._last_checksum = None
        self._last_change_ts = time.time()

        try:
            while not self.stopping:
                loop_start = time.time()
                ok, frame = False, None
                try:
                    ok, frame = self._timed_read(cap)
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("cap.read() 例外", tag, exc)
                    ok, frame = False, None

                # 【異常偵測機制 A】：解碼失敗
                if not ok or frame is None:
                    failed_reads += 1
                    if failed_reads >= DECODE_FAIL_LIMIT:
                        self.logger.warning(f"連續解碼失敗 {failed_reads} 次，判定串流異常", tag)
                        debug_print(f"解碼失敗 {failed_reads} 次 -> 重新開流", tag)
                        self._shm_writer.paint_warning(
                            ["畫面解碼失敗", "Decode Failed", self.camera.cam_name]
                        )
                        break
                    time.sleep(0.05)
                    continue
                failed_reads = 0

                frame = self._fit_frame(frame)
                last_frame_ts = time.time()

                # 【異常偵測機制 B】：畫面凍結超過 5 秒
                if not self._check_freeze(frame):
                    self.logger.warning(
                        f"畫面凍結超過 {FREEZE_THRESHOLD_SEC:.0f} 秒，判定串流異常", tag
                    )
                    debug_print(f"畫面凍結 > {FREEZE_THRESHOLD_SEC:.0f}s -> 重新開流", tag)
                    self._shm_writer.paint_warning(
                        ["畫面凍結", "Frame Frozen", self.camera.cam_name]
                    )
                    break

                # 寫入 MP4（BGR 由 OpenCV 直接寫入，無額外壓縮轉換）
                try:
                    self._segment.write(frame)
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("錄影寫入例外", tag, exc)

                # 高速色彩空間轉換（OpenCV BGR -> PyQt 偏好的 RGB）
                try:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    self._shm_writer.write_frame(rgb)
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("共享記憶體寫入例外", tag, exc)

                # 限制最高約 25 FPS，避免過度消耗 CPU
                elapsed = time.time() - loop_start
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)

                # 串流穩定超過 30 秒：重置重連等待時間
                if (time.time() - session_start >= RECONNECT_RESET_AFTER_SEC
                        and self._reconnect_wait > RECONNECT_BASE_WAIT):
                    self._reconnect_wait = RECONNECT_BASE_WAIT
                    self.logger.debug("串流穩定，重連等待時間已重置", tag)

                # 保護：太久沒有新影格
                if time.time() - last_frame_ts > FREEZE_THRESHOLD_SEC:
                    self.logger.warning("逾時未取得新影格，重新開流", tag)
                    self._shm_writer.paint_warning(
                        ["串流逾時", "Stream Timeout", self.camera.cam_name]
                    )
                    break
        finally:
            # 釋放資源並嘗試重新開流
            self._segment.close_current(reason="stream reset")
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
            self.logger.warning("串流已釋放，準備重新開流", tag)

        if not self.stopping:
            self._sleep_reconnect()

    # ------------------------------------------------------------------ #
    def _check_freeze(self, frame: np.ndarray) -> bool:
        """以縮圖 checksum 判斷畫面是否凍結（超過 FREEZE_THRESHOLD_SEC 才判定）。

        回傳 True 表示畫面正常（有變化或尚未逾時）；False 表示判定凍結。
        """
        now = time.time()
        thumb = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_NEAREST)
        checksum = int(np.sum(thumb, dtype=np.uint64))

        if self._last_checksum is None or checksum != self._last_checksum:
            self._last_checksum = checksum
            self._last_change_ts = now
            return True

        self._last_checksum = checksum
        if now - self._last_change_ts >= FREEZE_THRESHOLD_SEC:
            self._last_change_ts = now
            return False
        return True

    def _fit_frame(self, frame: np.ndarray) -> np.ndarray:
        """確保影格尺寸符合 config.json 設定（共享記憶體佈局固定）。"""
        if frame.shape[0] == self.camera.height and frame.shape[1] == self.camera.width:
            return frame
        return cv2.resize(frame, (self.camera.width, self.camera.height),
                          interpolation=cv2.INTER_LINEAR)

    def _sleep_reconnect(self) -> None:
        """重連等待（指數退避）；成功穩定後由 _record_session 重置等待時間。"""
        wait = max(RECONNECT_BASE_WAIT, min(self._reconnect_wait, RECONNECT_MAX_WAIT))
        self.logger.info(f"重連等待 {wait:.1f} 秒...", self.camera.cam_name)
        debug_print(f"reconnect wait {wait:.1f}s", self.camera.cam_name)
        # 使用 event.wait 讓外部 QUIT 可即時中斷等待
        self._state = "WAIT"
        self._stop_event.wait(wait)
        self._reconnect_wait = min(self._reconnect_wait * 2.0, RECONNECT_MAX_WAIT)


# =============================================================================
# 狀態監控（CMD 顯示每一路是否存活）
# =============================================================================
class StatusMonitor:
    """定期在 CMD 顯示每一路錄影執行緒的存活狀態（除錯資訊）。"""

    def __init__(self, threads: list, interval: float = 30.0) -> None:
        self.threads = threads
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="StatusMonitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval):
            statuses = []
            for rec in self.threads:
                # 以真實狀態顯示（連線/錄影/離線/等待），而非僅執行緒是否存活。
                # 舊版用 is_alive() 會讓「斷線重連中」的攝影機仍顯示 ALIVE，誤導操作人員。
                if not rec.is_alive():
                    label = "STOPPED"
                else:
                    label = getattr(rec, "state", "ALIVE")
                statuses.append(f"{rec.camera.zero_padded_id}:{label}")
            debug_print("[STATUS] " + " | ".join(statuses))


# =============================================================================
# 錄影管理器（主行程）
# =============================================================================
class RecorderManager:
    """讀取設定、啟動所有錄影執行緒，並負責優雅關閉（QUIT）。"""

    def __init__(self, root_folder: str, config_path: Optional[str] = None) -> None:
        self.root_folder = os.path.abspath(root_folder)
        self.config_path = config_path
        self.logger = DailyLogger(os.path.join(self.root_folder, "logs"))
        self.config: Optional[RecorderConfig] = None
        self.threads: list = []
        self._monitor: Optional[StatusMonitor] = None

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """啟動流程；成功回傳 True。"""
        self.logger.start_banner()
        debug_print("=" * 70)
        debug_print(f"{APP_NAME} 啟動中 ...")
        debug_print("=" * 70)

        try:
            self.config = load_recorder_config(self.root_folder, self.logger, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("讀取 config.json 失敗", exc=exc)
            debug_print(f"[ERROR] 讀取 config.json 失敗: {exc}")
            return False

        debug_print(f"root_folder : {self.config.root_folder}")
        debug_print(f"config file : {self.config.config_path}")
        debug_print(f"輸出根目錄 : {self.config.output_directory}")
        debug_print(f"今日錄影夾 : {self.config.today_dir}")
        debug_print(f"攝影機數量 : {self.config.camera_count}")

        for camera in self.config.cameras:
            debug_print(
                f"  Cam {camera.zero_padded_id} | {camera.cam_name} | {camera.brand} {camera.model} | "
                f"{camera.width}x{camera.height} | {mask_url(camera.rtsp_url)}",
                camera.cam_name,
            )
            recorder = CameraRecorderThread(camera, self.config.output_directory, self.logger)
            recorder.start()
            self.threads.append(recorder)
            self.logger.debug(f"已啟動錄影執行緒 {recorder.name}", camera.cam_name)

        monitor = StatusMonitor(self.threads)
        monitor.start()
        self._monitor = monitor

        debug_print("-" * 70)
        debug_print("所有錄影執行緒已啟動。輸入 QUIT 可停止錄影並結束程式。")
        debug_print("-" * 70)
        return True

    # ------------------------------------------------------------------ #
    def run_console_loop(self) -> None:
        """在 CMD 等待用戶指令，輸入 QUIT 停止錄影。"""
        while True:
            try:
                command = input()
            except (EOFError, KeyboardInterrupt):
                debug_print("偵測到中斷訊號，準備停止錄影 ...")
                break

            normalized = (command or "").strip().upper()
            if normalized == "QUIT":
                debug_print("收到 QUIT 指令，停止錄影 ...")
                self.logger.info("收到 QUIT 指令，停止錄影")
                break
            if normalized:
                debug_print(f"未知指令: {command}（輸入 QUIT 可停止錄影）")

    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        """停止所有執行緒並釋放共享記憶體。"""
        debug_print("正在停止所有錄影執行緒 ...")
        for recorder in self.threads:
            recorder.request_stop()

        deadline = time.time() + 15.0
        for recorder in self.threads:
            remaining = max(0.5, deadline - time.time())
            recorder.join(timeout=remaining)
            if recorder.is_alive():
                self.logger.warning("錄影執行緒未在期限內結束（daemon，隨行程退出）", recorder.name)
                debug_print(f"[WARN] {recorder.name} 未即時結束", recorder.camera.cam_name)

        try:
            if self._monitor is not None:
                self._monitor.stop()
        except Exception:  # noqa: BLE001
            pass

        debug_print("所有資源已釋放。")
        self.logger.end_banner()


# =============================================================================
# 進入點
# =============================================================================
def main(argv: Optional[list] = None) -> int:
    argv = list(argv if argv is not None else sys.argv)

    script_path = os.path.abspath(argv[0]) if argv else os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)

    # ---- 兩階段設定解析 ----------------------------------------------------
    # 1. 先從「同一資料夾的 config.json」讀取 root_folder
    launcher_config = os.path.join(script_dir, CONFIG_FILE_NAME)
    root_folder = resolve_root_folder(script_dir, argv)
    if os.path.isfile(launcher_config):
        declared_root = read_root_folder(launcher_config)
        if declared_root:
            root_folder = declared_root

    # 2. 再用 [root_folder] + \config\config.json 讀取攝影機設定
    camera_config = os.path.join(root_folder, CONFIG_DIR_NAME, CONFIG_FILE_NAME)
    if not os.path.isfile(camera_config):
        fallback = locate_config_file(root_folder, script_dir)
        camera_config = fallback if fallback else camera_config

    debug_print(f"Python      : {sys.version.split()[0]}")
    debug_print(f"OpenCV      : {cv2.__version__}")
    debug_print(f"NumPy       : {np.__version__}")
    debug_print(f"Launcher cfg: {launcher_config}")
    debug_print(f"root_folder : {root_folder}")
    debug_print(f"Config file : {camera_config}")

    manager: Optional[RecorderManager] = None
    try:
        manager = RecorderManager(root_folder, camera_config)
        if not manager.start():
            return 1
        manager.run_console_loop()
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] {exc}", flush=True)
        traceback.print_exc()
        if manager is not None:
            manager.logger.error("主行程發生未預期例外", exc=exc)
        return 1
    finally:
        if manager is not None:
            manager.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
    