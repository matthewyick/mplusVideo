"""
mplusVideo_Recorder_Console.py
==============================

多路 IP 攝影機錄影系統的 PyQt6 前端監控台。

架構（與 mplusVideo_Recorder_2025.py 後端完全解耦）
------------------------------------------------
* 前端只「唯讀」後端寫在作業系統共享記憶體的原始影像矩陣：
    - axis_stream_shm_XX   : RGB24 Raw Frame (height * width * 3)
    - axis_stream_meta_XX  : uint64[0]=frame counter / uint64[1]=status(1=OK,0=WARNING) / uint64[2]=unix timestamp
* Zero-copy：直接把共享記憶體 pointer 包成 QImage，不做任何複製。
* FastTransformation：多路縮放使用鄰近點插值，節省大量 UI CPU。
* 前端關閉時只 close() 記憶體映射，【絕不 unlink】——保護後端錄影不中斷。

啟動工作流程（依需求說明書）
--------------------------
1. 清除所有舊有的共享記憶體（僅在啟動時、後端尚未開始前）。
2. 檢查系統是否可呼叫 ffmpeg / ffprobe；缺少則彈窗提示。
3. 檢查與本程式同路徑的 config.json：
   - 若不存在，彈出對話框請使用者輸入本地儲存路徑（root_folder）。
   - 驗證可建立，失敗顯示錯誤要求重輸或退出。
   - 成功則在該路徑建立 config / logs 兩個資料夾，
     並於本程式同路徑寫入 config.json（內容為 {"root_folder": 值}）。
4. 若 (root_folder)/config/config.json 不存在，彈出視窗請使用者輸入 1~6 個 RTSP URL
   （最少一個、須為正確格式、防注入），依規格產生欄位後儲存。
5. 啟動介面，依 config.json 的 RTSP 數量以網格呈現 1~6 路畫面。
6. 介面提供「Start Recording / Stop Recording」切換按鈕，用 CMD 啟動/停止
   同路徑的 mplusVideo_Recorder_2025.py。
7. 介面提供「Summary」按鈕，展出本次錄影的日誌與錯誤信息。

執行：
    python mplusVideo_Recorder_Console.py

限制：
    * 不使用 global。
    * 不使用相依性注入；所有設定值以建構子 / 方法參數傳遞。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing.shared_memory import SharedMemory
from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# =============================================================================
# 與 mplusVideo_Recorder_2025.py 對齊的常數
# =============================================================================
APP_NAME = "mplusVideo_Recorder_Console"
BACKEND_SCRIPT_NAME = "mplusVideo_Recorder_2025.py"

CONFIG_DIR_NAME = "config"
CONFIG_FILE_NAME = "config.json"
ROOT_FOLDER_ENV = "MPLUSVIDEO_ROOT"
LOG_DIR_NAME = "logs"

SHM_PREFIX = "axis_stream_shm"
SHM_META_PREFIX = "axis_stream_meta"

META_CELLS = 3
STATUS_OK = 1
STATUS_WARNING = 0

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MAX_CAMERAS = 6
MIN_CAMERAS = 1

# ffmpeg / ffprobe（可由環境變數覆寫，與後端一致）
FFMPEG_BIN = os.environ.get("MPLUSVIDEO_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("MPLUSVIDEO_FFPROBE", "ffprobe")

# UI 更新頻率約 25~30 FPS；未連線時退回慢速重試
REFRESH_INTERVAL_MS = 33
# 後端還沒開時，3 秒後再嘗試，確保前端不閃退
RETRY_INTERVAL_MS = 3000
# 畫面凍結判定
STALE_THRESHOLD_SEC = 5.0

# RTSP URL 合法字元白名單（防注入）
_RTSP_URL_RE = re.compile(
    r"^rtsps?://"
    r"(?P<authority>[^\s/?#]+)"      # user:pass@host:port
    r"(?P<path>/[^\s]*)?"            # /path?query
    r"$",
    re.IGNORECASE,
)
# URL 中禁止的危險字元（shell 注入 / 控制字元）
# 注意：'&' 是合法的 RTSP 查詢字串分隔符（如 ?channel=1&subtype=0），不可封鎖。
# 我們全程以 subprocess list 形式（不經 shell）呼叫，故 '&' 不會造成指令注入。
_UNSAFE_URL_CHARS = set('`"\'<>|;$\\\r\n\t\0')


# =============================================================================
# 日誌（符合 Specification.md 第 6、7 節）
# =============================================================================
class ConsoleLogger:
    """寫入 logs/YYYY-MM-DD.log 與 error-YYYY-MM-DD.log（附加、不覆蓋）。"""

    def __init__(self, log_folder: str, app_name: str = APP_NAME) -> None:
        self.log_folder = os.path.abspath(log_folder)
        self.app_name = app_name
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            if not os.path.isdir(self.log_folder):
                os.makedirs(self.log_folder, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _append(self, filename: str, text: str) -> None:
        self._ensure_dir()
        path = os.path.join(self.log_folder, filename)
        try:
            if not os.path.exists(path):
                with open(path, "a", encoding="utf-8"):
                    pass
            with open(path, "a", encoding="utf-8") as fp:
                fp.write(text + "\n")
        except Exception:  # noqa: BLE001
            pass

    def log(self, level: str, message: str, exc: Optional[BaseException] = None) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {message}"
        if exc is not None:
            line += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._append(f"{self._today()}.log", line)
        if level in ("ERROR", "CRITICAL"):
            self.append_error(message, exc)

    def debug(self, message: str) -> None:
        self.log("DEBUG", message)

    def info(self, message: str) -> None:
        self.log("INFO", message)

    def warning(self, message: str) -> None:
        self.log("WARNING", message)

    def error(self, message: str, exc: Optional[BaseException] = None) -> None:
        self.log("ERROR", message, exc)

    def append_error(self, message: str, exc: Optional[BaseException] = None) -> None:
        now = datetime.now()
        body = f"=== [{now.strftime('%H:%M:%S')}] [{self.app_name}] ERROR {message}"
        if exc is not None:
            body += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._append(f"error-{self._today()}.log", body + "\n")

    def start_banner(self) -> None:
        self.info(f"[START TIME] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 前端監控台啟動")

    def end_banner(self) -> None:
        self.info(f"[END TIME] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 前端監控台結束")

    # ------------------------------------------------------------------ #
    def today_log_text(self) -> str:
        """回傳當日主日誌 + 錯誤日誌內容，供 Summary 顯示。"""
        parts = []
        for filename, title in ((f"{self._today()}.log", "主日誌"),
                                (f"error-{self._today()}.log", "錯誤日誌")):
            path = os.path.join(self.log_folder, filename)
            parts.append(f"===== {title} ({filename}) =====")
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fp:
                        parts.append(fp.read().strip() or "(空)")
                except Exception as exc:  # noqa: BLE001
                    parts.append(f"(讀取失敗: {exc})")
            else:
                parts.append("(不存在)")
        return "\n\n".join(parts)


def debug_print(message: str) -> None:
    """在 CMD 展出除錯信息。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def mask_url(url: str) -> str:
    """遮蔽 RTSP URL 的帳號密碼。"""
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


# =============================================================================
# 設定資料結構
# =============================================================================
@dataclass(frozen=True)
class CameraConfig:
    """單一攝影機設定（對應共享記憶體）。"""

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
        return self.width * self.height * 3


@dataclass(frozen=True)
class ConsoleConfig:
    cameras: list = field(default_factory=list)
    output_directory: str = ""
    today_dir: str = ""
    root_folder: str = ""
    config_path: str = ""
    log_folder: str = ""

    @property
    def camera_count(self) -> int:
        return len(self.cameras)


# =============================================================================
# 路徑與設定工具
# =============================================================================
def _repair_windows_path(value: str) -> str:
    """修復 JSON 中未轉義的反斜線所造成的換行／跳脫字元。"""
    if not value:
        return value
    repaired = value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    repaired = "".join(ch if ord(ch) >= 32 else "\\" for ch in repaired)
    return value if os.path.isdir(value) else repaired


def read_root_folder(config_path: str) -> Optional[str]:
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
    return os.path.abspath(_repair_windows_path(candidate))


def locate_config_file(root_folder: str, script_dir: str = "") -> Optional[str]:
    candidates = [os.path.join(root_folder, CONFIG_DIR_NAME, CONFIG_FILE_NAME)]
    if script_dir:
        candidates.append(os.path.join(script_dir, CONFIG_DIR_NAME, CONFIG_FILE_NAME))
        candidates.append(os.path.join(script_dir, CONFIG_FILE_NAME))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_root_folder(script_dir: str) -> str:
    env_root = os.environ.get(ROOT_FOLDER_ENV)
    if env_root and env_root.strip():
        return os.path.abspath(env_root.strip())
    parent = os.path.dirname(os.path.abspath(script_dir))
    if os.path.isfile(os.path.join(parent, CONFIG_DIR_NAME, CONFIG_FILE_NAME)):
        return parent
    return os.path.abspath(script_dir)


def _parse_camera(raw: dict, index: int) -> CameraConfig:
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
    """建立 root/YYYY/MM/DD 三層今日資料夾並回傳。"""
    when = when or datetime.now()
    target = os.path.join(root, when.strftime("%Y"), when.strftime("%m"), when.strftime("%d"))
    os.makedirs(target, exist_ok=True)
    return target


def load_console_config(root_folder: str, logger: ConsoleLogger,
                        config_path: Optional[str] = None) -> ConsoleConfig:
    """讀取 [root_folder]/config/config.json 並建立今日錄影資料夾。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if not config_path:
        config_path = locate_config_file(root_folder, script_dir)
    if not config_path or not os.path.isfile(config_path):
        raise FileNotFoundError(
            f"找不到設定檔: {os.path.join(root_folder, CONFIG_DIR_NAME, CONFIG_FILE_NAME)}"
        )

    real_root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    if not os.path.normcase(real_root).startswith(os.path.normcase(os.path.abspath(root_folder))):
        root_folder = real_root
    root_folder = os.path.abspath(root_folder)

    with open(config_path, "r", encoding="utf-8-sig") as fp:
        raw = json.load(fp)

    cameras_raw = raw.get("ip_cameras", []) or []
    cameras = [_parse_camera(item, idx) for idx, item in enumerate(cameras_raw)]
    if len(cameras) > MAX_CAMERAS:
        cameras = cameras[:MAX_CAMERAS]

    output_directory = str(raw.get("output_directory", "")).strip()
    if not output_directory:
        raise ValueError("config.json 缺少 output_directory")
    output_directory = os.path.normpath(_repair_windows_path(output_directory))

    os.makedirs(output_directory, exist_ok=True)
    today_dir = build_output_directory(output_directory)

    logger.debug(f"root_folder : {root_folder}")
    logger.debug(f"config file : {config_path}")
    logger.debug(f"今日錄影夾 : {today_dir}")
    logger.debug(f"攝影機數量 : {len(cameras)}")

    return ConsoleConfig(
        cameras=cameras,
        output_directory=output_directory,
        today_dir=today_dir,
        root_folder=root_folder,
        config_path=config_path,
        log_folder=os.path.join(root_folder, LOG_DIR_NAME),
    )


# =============================================================================
# 系統檢查與清理工具
# =============================================================================
def tool_available(executable: str) -> bool:
    """檢查可執行程式是否可在 PATH 中找到（或為存在的完整路徑）。"""
    try:
        if shutil.which(executable):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        if os.path.isfile(executable):
            return True
    except Exception:  # noqa: BLE001
        pass
    # 直接在 CMD 試呼叫（-version），能執行才算真的可用
    try:
        subprocess.run(
            [executable, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def check_ffmpeg_tools() -> tuple:
    """回傳 (ffmpeg_ok, ffprobe_ok)。"""
    return tool_available(FFMPEG_BIN), tool_available(FFPROBE_BIN)


def clear_stale_shared_memory(cam_count: int, logger: Optional[ConsoleLogger] = None) -> list:
    """啟動時清除 1~MAX_CAMERAS 路徑的舊共享記憶體（best-effort）。

    僅在啟動、後端尚未開始時呼叫；正常關閉時【絕不】呼叫（保護後端）。
    若某區段正被其它行程持有而無法移除，忽略錯誤（後端會自動相容重用）。
    """
    removed = []
    upper = max(cam_count, MAX_CAMERAS)
    for cam in range(1, upper + 1):
        cam_id = str(cam).zfill(2)
        for name in (f"{SHM_PREFIX}_{cam_id}", f"{SHM_META_PREFIX}_{cam_id}"):
            try:
                shm = SharedMemory(name=name, create=False)
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001
                continue
            try:
                shm.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                shm.unlink()
                removed.append(name)
            except Exception:  # noqa: BLE001
                pass
    if logger is not None:
        logger.debug(f"啟動清除舊共享記憶體: {removed or '無'}")
    return removed


def validate_rtsp_url(url: str) -> bool:
    """嚴格驗證 RTSP URL 格式並防止注入。"""
    if not url:
        return False
    url = url.strip()
    if len(url) > 2048:
        return False
    if any(ch in _UNSAFE_URL_CHARS for ch in url):
        return False
    if any(ord(ch) < 32 for ch in url):
        return False
    if not url.lower().startswith("rtsp"):
        return False
    return _RTSP_URL_RE.match(url) is not None


def write_json_file(path: str, data: dict) -> None:
    """以 UTF-8 寫入 JSON（保留中文、縮排易讀）。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def build_camera_config_dicts(urls: list) -> list:
    """依規格以輸入匣 index 產生 ip_cameras 內容。"""
    cameras = []
    for index, url in enumerate(urls, start=1):
        cameras.append({
            "cam_id": index,
            "cam_name": f"camera{index}",
            "rtsp_url": url,
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "brand": "AXIS",
            "model": "1349",
            "compression": "265",
        })
    return cameras


# =============================================================================
# 共享記憶體讀取器（Zero-copy、關閉時只 close 不 unlink）
# =============================================================================
class SharedFrameReader:
    """掛載既有的作業系統共享記憶體並映射為 Numpy 視圖（Zero-copy）。

    前端只在關閉時 close()，絕不 unlink()。
    """

    def __init__(self, camera: CameraConfig) -> None:
        self.camera = camera
        self._shm: Optional[SharedMemory] = None
        self._meta: Optional[SharedMemory] = None
        self._frame_view: Optional[np.ndarray] = None
        self._meta_view: Optional[np.ndarray] = None
        self.last_counter = -1
        self.last_timestamp = 0.0
        self.status = STATUS_WARNING

    # ------------------------------------------------------------------ #
    @property
    def attached(self) -> bool:
        return self._frame_view is not None and self._meta_view is not None

    def try_attach(self) -> bool:
        """嘗試掛載已存在的共享記憶體；不存在回傳 False（不丟例外）。"""
        if self.attached:
            return True
        try:
            frame_shm = SharedMemory(name=self.camera.shm_name, create=False)
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001
            return False

        try:
            meta_shm = SharedMemory(name=self.camera.shm_meta_name, create=False)
        except Exception:  # noqa: BLE001
            try:
                frame_shm.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        if frame_shm.size < self.camera.frame_bytes or meta_shm.size < META_CELLS * 8:
            try:
                frame_shm.close()
                meta_shm.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        try:
            self._frame_view = np.ndarray(
                (self.camera.height, self.camera.width, 3),
                dtype=np.uint8,
                buffer=frame_shm.buf,
                offset=0,
            )
            self._meta_view = np.ndarray(
                (META_CELLS,), dtype=np.uint64, buffer=meta_shm.buf, offset=0
            )
        except Exception:  # noqa: BLE001
            try:
                frame_shm.close()
                meta_shm.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._shm = frame_shm
        self._meta = meta_shm
        return True

    # ------------------------------------------------------------------ #
    def read_meta(self) -> tuple:
        """回傳 (counter, status, timestamp)；未掛載回傳 (-1, WARNING, 0)。"""
        if self._meta_view is None:
            return -1, STATUS_WARNING, 0.0
        try:
            counter = int(self._meta_view[0])
            status = int(self._meta_view[1])
            timestamp = float(self._meta_view[2])
            self.last_counter = counter
            self.status = status
            self.last_timestamp = timestamp
            return counter, status, timestamp
        except Exception:  # noqa: BLE001
            return -1, STATUS_WARNING, 0.0

    def frame_view(self) -> Optional[np.ndarray]:
        return self._frame_view

    def is_stale(self) -> bool:
        """時間戳超過 STALE_THRESHOLD_SEC 無更新視為凍結。"""
        if self.last_timestamp <= 0:
            return False
        return (time.time() - self.last_timestamp) > STALE_THRESHOLD_SEC

    # ------------------------------------------------------------------ #
    def release(self) -> None:
        """釋放前端映射：只 close()，【絕不 unlink】。"""
        for attr in ("_shm", "_meta"):
            shm = getattr(self, attr, None)
            if shm is None:
                continue
            try:
                shm.close()
            except Exception:  # noqa: BLE001
                pass
            setattr(self, attr, None)
        self._frame_view = None
        self._meta_view = None


# =============================================================================
# 單路畫面
# =============================================================================
class CameraView(QLabel):
    """單路畫面：Zero-copy QImage + FastTransformation 縮放。"""

    def __init__(self, camera: CameraConfig) -> None:
        super().__init__()
        self.camera = camera
        self.reader = SharedFrameReader(camera)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #101010; color: #d0d0d0;")
        self.setScaledContents(False)
        self.setText(f"{camera.zero_padded_id} ? {camera.cam_name}\n等待後端連線 ...")
        font = self.font()
        font.setPointSize(11)
        self.setFont(font)

    # ------------------------------------------------------------------ #
    def try_attach(self) -> bool:
        return self.reader.try_attach()

    def refresh(self) -> None:
        if not self.reader.attached:
            if not self.reader.try_attach():
                self._show_text(f"{self.camera.zero_padded_id} ? {self.camera.cam_name}\n"
                                f"等待後端連線 ...")
                return

        counter, status, _ts = self.reader.read_meta()
        frame = self.reader.frame_view()
        if frame is None:
            return

        stale = self.reader.is_stale()
        height, width = frame.shape[0], frame.shape[1]
        # Zero-copy：直接用記憶體指標建立 QImage，不複製
        image = QImage(frame.data, width, height, width * 3, QImage.Format.Format_RGB888)

        pixmap = QPixmap.fromImage(image)
        target = self.size()
        if target.width() > 0 and target.height() > 0:
            # FastTransformation：鄰近點插值，節省大量 UI CPU
            scaled = pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        else:
            scaled = pixmap

        if stale or status == STATUS_WARNING:
            overlay = QPixmap(scaled.size())
            overlay.fill(Qt.GlobalColor.transparent)
            painter = QPainter(overlay)
            painter.drawPixmap(0, 0, scaled)
            painter.setPen(QColor(255, 60, 60))
            font = QFont()
            font.setPointSize(12)
            font.setBold(True)
            painter.setFont(font)
            label = "凍結" if stale else "警告"
            painter.drawText(
                overlay.rect(),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
                f"{label} | {self.camera.cam_name}",
            )
            painter.end()
            self.setPixmap(overlay)
        else:
            self.setPixmap(scaled)

    def _show_text(self, text: str) -> None:
        self.setPixmap(QPixmap())
        self.setText(text)

    def release(self) -> None:
        self.reader.release()


# =============================================================================
# 初始設定對話框
# =============================================================================
class RootFolderDialog(QDialog):
    """請使用者輸入本地儲存路徑（root_folder）。"""

    def __init__(self, default_path: str = "") -> None:
        super().__init__()
        self.setWindowTitle("設定儲存路徑")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("請輸入本地路徑，作為儲存影片與日誌之用："))
        self.edit = QLineEdit(default_path)
        self.edit.setPlaceholderText(r"例如：E:\root_mplusVideo2025")
        layout.addWidget(self.edit)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #ff6060;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._result = ""

    def _on_ok(self) -> None:
        path = self.edit.text().strip()
        if not path:
            self.error.setText("路徑不可為空。")
            return
        self._result = path
        self.accept()

    def value(self) -> str:
        return self._result


class RtspUrlsDialog(QDialog):
    """請使用者輸入 1~6 個 RTSP URL（最少一個、格式驗證、防注入）。"""

    def __init__(self, count: int = MAX_CAMERAS) -> None:
        super().__init__()
        self.setWindowTitle("輸入 RTSP URL")
        self.setMinimumWidth(680)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"請輸入 1 到 {MAX_CAMERAS} 個 RTSP URL（至少一個，格式須正確）。"
        ))
        form = QFormLayout()
        self.edits = []
        for idx in range(1, count + 1):
            edit = QLineEdit()
            edit.setPlaceholderText(
                "rtsp://user:password@192.168.1.50/axis-media/media.amp"
            )
            form.addRow(f"Cam {str(idx).zfill(2)}:", edit)
            self.edits.append(edit)
        layout.addLayout(form)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #ff6060;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._urls = []

    def _on_ok(self) -> None:
        # 收集每個輸入匣內容（保留「位置」-> cam_id 對應輸入匣 index）。
        # 允許尾端留空，但不允許中間留空（避免 cam_id 與輸入匣位置錯位）。
        raw_texts = [edit.text().strip() for edit in self.edits]
        last_filled = -1
        for i, t in enumerate(raw_texts):
            if t:
                last_filled = i
        if last_filled < 0:
            self.error.setText("至少需要輸入一個有效的 RTSP URL。")
            return

        urls = []
        for i in range(last_filled + 1):
            text = raw_texts[i]
            if not text:
                self.error.setText(
                    f"Cam {str(i + 1).zfill(2)} 為空白，請從 Cam 01 起連續填入（可留空尾端）。"
                )
                return
            if not validate_rtsp_url(text):
                self.error.setText(
                    f"Cam {str(i + 1).zfill(2)} 的 RTSP URL 格式不正確（僅允訸 rtsp:// 或 rtsps://）。"
                )
                return
            urls.append(text)

        if len(urls) < MIN_CAMERAS:
            self.error.setText("至少需要輸入一個有效的 RTSP URL。")
            return
        if len(urls) > MAX_CAMERAS:
            urls = urls[:MAX_CAMERAS]
        self._urls = urls
        self.accept()

    def urls(self) -> list:
        return self._urls


class SummaryDialog(QDialog):
    """展出本次錄影的日誌與錯誤信息。"""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.setWindowTitle("錄影摘要（日誌與錯誤）")
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


# =============================================================================
# 後端錄影行程控制
# =============================================================================
class BackendRecorder:
    """用 CMD 啟動/停止 mplusVideo_Recorder_2025.py（獨立行程，關閉前端不影響）。"""

    def __init__(self, script_path: str, logger: ConsoleLogger) -> None:
        self.script_path = os.path.abspath(script_path)
        self.logger = logger
        self._proc: Optional[subprocess.Popen] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """啟動後端；成功回傳 True。"""
        if self.running:
            return True
        if not os.path.isfile(self.script_path):
            raise FileNotFoundError(f"找不到後端程式: {self.script_path}")

        script_dir = os.path.dirname(self.script_path)
        flags = 0
        if os.name == "nt":
            # 獨立主控台視窗（等同 CMD 啟動），關閉本前端不影響
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

        # 以 stdin 管道下達 QUIT 以便優雅停止
        proc = subprocess.Popen(
            [sys.executable, "-u", self.script_path],
            cwd=script_dir,
            stdin=subprocess.PIPE,
            stdout=None,
            stderr=None,
            creationflags=flags,
        )
        self._proc = proc
        self.logger.info(f"已啟動後端錄影 PID={proc.pid}")
        debug_print(f"啟動後端錄影 PID={proc.pid}: {self.script_path}")
        return True

    def stop(self, timeout: float = 15.0) -> None:
        """下達 QUIT 並等待結束；逾時則強制終止。"""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b"QUIT\n")
                    proc.stdin.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=timeout)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        self._proc = None
        self.logger.info("後端錄影已停止")
        debug_print("後端錄影已停止")


# =============================================================================
# 主監控視窗
# =============================================================================
class MonitorWindow(QMainWindow):
    """1~6 路網格監控台 + Start/Stop Recording + Summary。"""

    def __init__(self, config: ConsoleConfig, logger: ConsoleLogger,
                 backend_script: str) -> None:
        super().__init__()
        self.config = config
        self.logger = logger
        self.backend = BackendRecorder(backend_script, logger)
        self.views: list = []
        self._recording_started_at: Optional[datetime] = None

        self.setWindowTitle(
            f"{APP_NAME} - {config.camera_count} 路 | {config.today_dir}"
        )
        self.resize(1280, 760)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # 頂部工具列
        toolbar = QHBoxLayout()
        self.record_btn = QPushButton("Start Recording")
        self.record_btn.setMinimumWidth(160)
        self.record_btn.clicked.connect(self._on_toggle_recording)
        toolbar.addWidget(self.record_btn)

        self.summary_btn = QPushButton("Summary")
        self.summary_btn.setMinimumWidth(120)
        self.summary_btn.clicked.connect(self._on_summary)
        toolbar.addWidget(self.summary_btn)

        self.state_label = QLabel("狀態：待機")
        self.state_label.setStyleSheet("color: #a0c0ff;")
        toolbar.addWidget(self.state_label)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        header = QLabel(
            f"root_folder: {config.root_folder}    今日錄影夾: {config.today_dir}"
        )
        header.setStyleSheet("color: #a0c0ff; font-size: 12px;")
        outer.addWidget(header)

        # 網格
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        columns = self._columns_for(config.camera_count)
        for index, camera in enumerate(config.cameras):
            view = CameraView(camera)
            row, col = divmod(index, columns)
            grid.addWidget(view, row, col)
            self.views.append(view)

        for col in range(columns):
            grid.setColumnStretch(col, 1)
        rows = max(1, (config.camera_count + columns - 1) // columns)
        for row in range(rows):
            grid.setRowStretch(row, 1)

        outer.addWidget(grid_host, 1)
        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_refresh)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _columns_for(count: int) -> int:
        """1 -> 1 欄；2/3/4 -> 2 欄；5/6 -> 3 欄。"""
        if count <= 1:
            return 1
        if count <= 4:
            return 2
        return 3

    def start(self) -> None:
        """啟動記憶體對接；後端未開時每 3 秒重試，不閃退。"""
        attached = sum(1 for view in self.views if view.try_attach())
        debug_print(f"啟動記憶體對接: {attached}/{len(self.views)} 已掛載")
        if attached < len(self.views):
            debug_print("後端尚未啟動，3 秒後重試 ...")
        self._timer.start(REFRESH_INTERVAL_MS if attached else RETRY_INTERVAL_MS)

    def _on_refresh(self) -> None:
        any_attached = False
        for view in self.views:
            try:
                if view.reader.attached or view.try_attach():
                    any_attached = True
                view.refresh()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("畫面更新失敗", exc)

        desired = REFRESH_INTERVAL_MS if any_attached else RETRY_INTERVAL_MS
        if self._timer.interval() != desired:
            self._timer.setInterval(desired)

        # 後端若被外部關閉，回復按鈕狀態
        if self._recording_started_at is not None and not self.backend.running:
            self._set_recording_ui(False)

    # ------------------------------------------------------------------ #
    def _set_recording_ui(self, recording: bool) -> None:
        if recording:
            self.record_btn.setText("Stop Recording")
            self.record_btn.setStyleSheet("color: #ff5050; font-weight: bold;")
            self.state_label.setText(f"狀態：錄影中（自 {self._recording_started_at:%H:%M:%S}）")
        else:
            self.record_btn.setText("Start Recording")
            self.record_btn.setStyleSheet("color: #50d050; font-weight: bold;")
            self.state_label.setText("狀態：待機")
            self._recording_started_at = None

    def _on_toggle_recording(self) -> None:
        if self._recording_started_at is None:
            try:
                self.backend.start()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("啟動後端錄影失敗", exc)
                QMessageBox.critical(self, "錯誤", f"啟動後端錄影失敗：\n{exc}")
                return
            self._recording_started_at = datetime.now()
            self._set_recording_ui(True)
            self.logger.info("使用者按下 Start Recording")
        else:
            self.backend.stop()
            self._set_recording_ui(False)
            self.logger.info("使用者按下 Stop Recording")

    def _on_summary(self) -> None:
        text = self.logger.today_log_text()
        summary = SummaryDialog(text)
        summary.exec()

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """手動關閉：釋放前端記憶體映射，【絕不 unlink】。"""
        debug_print("監控視窗關閉，正在釋放前端記憶體連接 ...")
        self._timer.stop()
        for view in self.views:
            try:
                view.release()
            except Exception:  # noqa: BLE001
                pass
        debug_print("已釋放前端記憶體連接（不 unlink，保護後端錄影）")
        self.logger.info("前端關閉並釋放記憶體連接（未 unlink）")
        super().closeEvent(event)


# =============================================================================
# 啟動流程（ffmpeg 檢查 / 設定對話框）
# =============================================================================
def ensure_tools_gui() -> bool:
    """檢查 ffmpeg / ffprobe；缺少時彈窗。回傳是否全部可用。"""
    ffmpeg_ok, ffprobe_ok = check_ffmpeg_tools()
    if ffmpeg_ok and ffprobe_ok:
        return True
    missing = []
    if not ffmpeg_ok:
        missing.append("ffmpeg")
    if not ffprobe_ok:
        missing.append("ffprobe")
    QMessageBox.critical(
        None,
        "缺少必要軟體",
        "無法呼叫：" + "、".join(missing) + "\n\n"
        "請安裝 ffmpeg / ffprobe，並將其所在資料夾加入系統環境變數 PATH。\n"
        "下載：https://ffmpeg.org/download.html\n\n"
        "設定後請重新啟動本程式。",
    )
    return False


def ensure_root_folder_gui(script_dir: str, proposed_root: str) -> Optional[str]:
    """確保 root_folder 與 config.json 存在；回傳可用的 root_folder 或 None。"""
    launcher_config = os.path.join(script_dir, CONFIG_FILE_NAME)

    # 1) 已有啟動設定檔 -> 讀取 root_folder
    if os.path.isfile(launcher_config):
        existing = read_root_folder(launcher_config)
        if existing:
            return existing

    # 2) 沒有 -> 請使用者輸入，直到成功或取消
    default = proposed_root if proposed_root and os.path.isdir(proposed_root) else ""
    while True:
        dialog = RootFolderDialog(default)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        path = dialog.value()

        try:
            os.makedirs(path, exist_ok=True)
            if not os.path.isdir(path):
                raise OSError("無法建立資料夾")
            os.makedirs(os.path.join(path, CONFIG_DIR_NAME), exist_ok=True)
            os.makedirs(os.path.join(path, LOG_DIR_NAME), exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                None, "路徑錯誤",
                f"無法建立路徑：\n{path}\n\n{exc}\n請再輸入一次或按 Cancel 退出。",
            )
            default = path
            continue

        # 成功：寫入啟動設定檔
        try:
            write_json_file(launcher_config, {"root_folder": os.path.abspath(path)})
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(None, "寫入失敗", f"無法寫入 config.json：\n{exc}")
            return None
        debug_print(f"已建立 root_folder 與 launcher config.json: {path}")
        return os.path.abspath(path)


def ensure_camera_config_gui(root_folder: str) -> Optional[str]:
    """確保 (root_folder)/config/config.json 存在；回傳檔案路徑或 None。"""
    config_dir = os.path.join(root_folder, CONFIG_DIR_NAME)
    os.makedirs(config_dir, exist_ok=True)
    camera_config = os.path.join(config_dir, CONFIG_FILE_NAME)

    if os.path.isfile(camera_config):
        return camera_config

    # 沒有 -> 請使用者輸入 RTSP URL
    dialog = RtspUrlsDialog(MAX_CAMERAS)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    urls = dialog.urls()
    if not urls:
        return None

    output_dir = os.path.join(root_folder, "root_mplusVideo")
    data = {
        "server": {"ip_address": "127.0.0.1", "port": 80},
        "ip_cameras": build_camera_config_dicts(urls),
        "output_directory": output_dir,
    }
    try:
        write_json_file(camera_config, data)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, "寫入失敗", f"無法寫入 config.json：\n{exc}")
        return None
    debug_print(f"已建立攝影機設定檔: {camera_config}（{len(urls)} 路）")
    return camera_config


# =============================================================================
# 主程式
# =============================================================================
def _pyqt_version() -> str:
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR  # noqa: PLC0415
        return PYQT_VERSION_STR
    except Exception:  # noqa: BLE001
        return "unknown"


def main(argv: Optional[list] = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    script_path = os.path.abspath(argv[0]) if argv else os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    backend_script = os.path.join(script_dir, BACKEND_SCRIPT_NAME)

    app = QApplication(argv)

    # 1) 檢查必要軟體（ffmpeg / ffprobe）
    if not ensure_tools_gui():
        return 1

    # 2) 清除所有舊有的共享記憶體（啟動時）
    proposed_root = resolve_root_folder(script_dir)
    clear_stale_shared_memory(MAX_CAMERAS, None)

    # 3) 確保 root_folder / launcher config.json
    root_folder = ensure_root_folder_gui(script_dir, proposed_root)
    if not root_folder:
        debug_print("使用者取消設定，結束程式。")
        return 0

    logger = ConsoleLogger(os.path.join(root_folder, LOG_DIR_NAME))
    logger.start_banner()

    debug_print(f"Python      : {sys.version.split()[0]}")
    debug_print(f"PyQt6       : {_pyqt_version()}")
    debug_print(f"NumPy       : {np.__version__}")
    debug_print(f"root_folder : {root_folder}")

    # 4) 確保攝影機 config.json
    camera_config = ensure_camera_config_gui(root_folder)
    if not camera_config:
        debug_print("使用者取消輸入 RTSP URL，結束程式。")
        logger.end_banner()
        return 0
    debug_print(f"Config file : {camera_config}")

    # 5) 載入設定（建立今日三層資料夾）
    try:
        config = load_console_config(root_folder, logger, camera_config)
    except Exception as exc:  # noqa: BLE001
        logger.error("載入 config.json 失敗", exc)
        debug_print(f"[ERROR] 載入 config.json 失敗: {exc}")
        QMessageBox.critical(None, "設定錯誤", f"載入 config.json 失敗：\n{exc}")
        logger.end_banner()
        return 1

    debug_print(f"攝影機數量 : {config.camera_count}")
    for camera in config.cameras:
        debug_print(f"  Cam {camera.zero_padded_id} | {camera.cam_name} | "
                    f"{camera.width}x{camera.height} | {mask_url(camera.rtsp_url)}")

    if not os.path.isfile(backend_script):
        QMessageBox.warning(None, "找不到後端",
                            f"同路徑找不到後端程式：\n{backend_script}\n"
                            f"Start Recording 將無法啟動。")

    # 6) 啟動介面
    window = MonitorWindow(config, logger, backend_script)
    window.start()
    window.show()

    exit_code = app.exec()

    # 7) 關閉後釋放（只 close，不 unlink）
    try:
        window.backend.stop()
    except Exception:  # noqa: BLE001
        pass
    for view in window.views:
        try:
            view.release()
        except Exception:  # noqa: BLE001
            pass

    logger.end_banner()
    debug_print("前端監控台結束")
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
