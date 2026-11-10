"""
mplusVideo_Viewer_2025.py
=========================

多路 IP 攝影機「即時觀看影片與聲音」的 PyQt6 + python-mpv 播放器。

功能總覽
--------
* 使用 python-mpv（libmpv 硬體解碼）即時播放 RTSP 串流（1/2/4/6 路）。
* 左側窄選單：1、2、4、6（圖示），決定主畫面播放幾路。
* 每個播放匣固定 16:9，附音量拖曳棒、靜音鈕、大小縮放鈕、1:1 還原鈕。
* 聲音拖曳棒與靜音鈕互動同步。
* 自動互鎖音訊 (Audio Interlocking)：
    - 1 畫面：第 1 路聲音自動開啟。
    - 2 / 4 / 6 畫面：所有串流初始靜音。
    - 任一畫面「左鍵連點兩下」：獨佔開啟該路聲音（亮綠色邊框），其他靜音。
* 滑鼠框選縮放 (Click-to-Zoom)：
    - 左鍵拖曳矩形 -> 放開後 mpv 將該區域放大填滿。
    - 可無限次連續框選。
    - 右鍵 -> 一鍵還原 100%。
    - 滑鼠滾輪 -> 以游標為中心縮放。
* 全螢幕優化與懸浮控制 (HUD)：
    - 「🖥️ 全螢幕放大所選」將亮綠框（有聲音）畫面放大至全螢幕。
    - 全螢幕時上方中央掉下半透明控制面板（切換聲音 / 退出全螢幕）。
    - 全螢幕下仍可左鍵畫框縮放、右鍵重設。

* 讀取同資料夾的 viewer_config.json；若無則彈出對話框輸入 1~6 個 RTSP URL
  （防注入），並儲存 viewer_config.json。

執行：
    python mplusVideo_Viewer_2025.py

限制：
    * 不使用 global。
    * 不使用相依性注入；設定值以建構子 / 方法參數傳遞。

相依：
    * python-mpv（需要系統 libmpv：Windows 需 mpv-2.dll / libmpv-2.dll）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

# 在 import mpv 之前，先嘗試把 libmpv 所在資料夾加入 PATH
def _ensure_libmpv_path(script_dir: str) -> None:
    """把可能含 libmpv 的資料夾加入 PATH，讓 python-mpv 能找到 dll。"""
    candidates = []
    if script_dir:
        candidates.append(script_dir)
    for env in ("MPLUSVIDEO_LIBMPV_DIR", "MPV_HOME"):
        val = os.environ.get(env)
        if val and os.path.isdir(val):
            candidates.append(val)
    # 常見安裝位置（Windows）
    for base in (
        r"C:\mpv",
        r"C:\Program Files\mpv",
        r"C:\Program Files (x86)\mpv",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "mpv"),
    ):
        if base and os.path.isdir(base):
            candidates.append(base)
    added = []
    for path in candidates:
        if path and os.path.isdir(path):
            if path not in os.environ.get("PATH", ""):
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
                added.append(path)
    if added:
        print(f"[DEBUG] 已將 libmpv 可能路徑加入 PATH: {added}", flush=True)


def _try_import_mpv(script_dir: str):
    """嘗試載入 python-mpv；失敗回傳 (None, 錯誤訊息)。"""
    _ensure_libmpv_path(script_dir)
    try:
        import mpv  # noqa: PLC0415
        return mpv, ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


from PyQt6.QtCore import Qt, QTimer, QPoint, QRect, QEvent, pyqtSignal  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor,
    QCursor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# =============================================================================
# 常數
# =============================================================================
APP_NAME = "mplusVideo_Viewer_2025"
# 本播放器專屬設定檔（與錄影端的 config.json 區隔）
CONFIG_FILE_NAME = "viewer_config.json"

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

MAX_CAMERAS = 6
MIN_CAMERAS = 1
LAYOUT_CHOICES = (1, 2, 4, 6)

# RTSP URL 驗證（防注入）
_RTSP_URL_RE = re.compile(
    r"^rtsps?://"
    r"(?P<authority>[^\s/?#]+)"
    r"(?P<path>/[^\s]*)?"
    r"$",
    re.IGNORECASE,
)
# 注意：'&' 是合法 RTSP 查詢分隔符，不可封鎖（全程不經 shell）
_UNSAFE_URL_CHARS = set('`"\'<>|;$\\\r\n\t\0')

# mpv 屬性追蹤的縮放範圍
ZOOM_MIN = 1.0
ZOOM_MAX = 12.0


# =============================================================================
# 設定資料
# =============================================================================
class CameraEntry:
    """單一攝影機設定（僅 Viewer 需要的欄位）。"""

    def __init__(self, cam_id: int, cam_name: str, rtsp_url: str) -> None:
        self.cam_id = cam_id
        self.cam_name = cam_name
        self.rtsp_url = rtsp_url

    @property
    def zero_padded_id(self) -> str:
        return str(self.cam_id).zfill(2)


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


def load_cameras(config_path: str) -> list:
    """讀取 viewer_config.json 的 ip_cameras，回傳 CameraEntry 清單。"""
    with open(config_path, "r", encoding="utf-8-sig") as fp:
        data = json.load(fp)
    raw_cams = data.get("ip_cameras", []) or []
    cameras = []
    for index, raw in enumerate(raw_cams):
        if not isinstance(raw, dict):
            continue
        cam_id = raw.get("cam_id", index + 1)
        try:
            cam_id = int(cam_id)
        except (TypeError, ValueError):
            cam_id = index + 1
        cam_name = str(raw.get("cam_name", f"camera{cam_id}"))
        rtsp_url = str(raw.get("rtsp_url", ""))
        if rtsp_url:
            cameras.append(CameraEntry(cam_id, cam_name, rtsp_url))
        if len(cameras) >= MAX_CAMERAS:
            break
    return cameras


def write_config(config_path: str, urls: list) -> None:
    """依規格寫入 viewer_config.json（ip_cameras + 預設欄位）。"""
    cameras = []
    for index, url in enumerate(urls, start=1):
        cameras.append({
            "cam_id": index,
            "cam_name": f"camera{index}",
            "rtsp_url": url,
            "width": 1920,
            "height": 1080,
            "brand": "AXIS",
            "model": "1349",
            "compression": "265",
        })
    data = {
        "server": {"ip_address": "127.0.0.1", "port": 80},
        "ip_cameras": cameras,
        "output_directory": os.path.join(os.path.dirname(os.path.abspath(config_path)),
                                         "root_mplusVideo"),
    }
    with open(config_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def debug_print(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# =============================================================================
# RTSP URL 輸入對話框（1~6 個輸入匣，防注入）
# =============================================================================
class RtspInputDialog(QDialog):
    """請使用者輸入 1~6 個 RTSP URL（最少一個、格式驗證、防注入）。"""

    def __init__(self, existing: list = None) -> None:
        super().__init__()
        self.setWindowTitle("輸入 RTSP URL")
        self.setMinimumWidth(720)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"請輸入 1 到 {MAX_CAMERAS} 個 RTSP URL（至少一個，格式須正確，從 Cam 01 起連續）。"
        ))
        form = QFormLayout()
        self.edits = []
        existing = existing or []
        for idx in range(1, MAX_CAMERAS + 1):
            edit = QLineEdit()
            edit.setPlaceholderText(
                "rtsp://user:password@192.168.1.50/axis-media/media.amp"
            )
            if idx <= len(existing):
                edit.setText(existing[idx - 1])
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
        raw = [e.text().strip() for e in self.edits]
        last_filled = -1
        for i, t in enumerate(raw):
            if t:
                last_filled = i
        if last_filled < 0:
            self.error.setText("至少需要輸入一個有效的 RTSP URL。")
            return
        urls = []
        for i in range(last_filled + 1):
            text = raw[i]
            if not text:
                self.error.setText(
                    f"Cam {str(i + 1).zfill(2)} 為空白，請從 Cam 01 起連續填入（可留空尾端）。"
                )
                return
            if not validate_rtsp_url(text):
                self.error.setText(
                    f"Cam {str(i + 1).zfill(2)} 的 RTSP URL 格式不正確（僅允許 rtsp:// 或 rtsps://）。"
                )
                return
            urls.append(text)
        if len(urls) < MIN_CAMERAS:
            self.error.setText("至少需要輸入一個有效的 RTSP URL。")
            return
        self._urls = urls[:MAX_CAMERAS]
        self.accept()

    def urls(self) -> list:
        return self._urls


# =============================================================================
# 單一播放匣（內嵌 mpv / 16:9 / 音量 / 靜音 / 縮放 / 1:1 / 框選縮放）
# =============================================================================
class VideoPane(QWidget):
    """一個 16:9 的播放匣，內嵌一個獨立 mpv 實例。

    * 影片畫面嵌入原生子視窗（mpv wid）。
    * 控制列：音量拖曳棒、靜音鈕、縮放鈕、1:1 還原鈕。
    * 左鍵拖曳 == 框選縮放；右鍵 == 還原；左鍵連點兩下 == 聲音獨佔。
    """

    # 訊號：要求此畫面獨佔聲音
    request_audio = pyqtSignal(object)
    # 訊號：請求全螢幕此畫面
    request_fullscreen = pyqtSignal(object)

    def __init__(self, camera: CameraEntry, mpv_module) -> None:
        super().__init__()
        self.camera = camera
        self.mpv_module = mpv_module
        self.player = None
        self.active_audio = False
        self._last_volume = 60
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_start: QPoint = None
        self._drag_cur: QPoint = None
        self._selecting = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 200)
        self.setStyleSheet("background-color: #000000;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 影片容器（mpv 會嵌入此 widget 的原生子視窗）
        self.video_host = QWidget(self)
        self.video_host.setStyleSheet("background-color: #000000;")
        self.video_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_host.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)

        # 疊在最上層的「框選」透明層 + 標題
        self.title_label = QLabel(self)
        self.title_label.setText(f"{camera.zero_padded_id} ? {camera.cam_name}")
        self.title_label.setStyleSheet(
            "color: #d0e0ff; background-color: rgba(0,0,0,120); "
            "padding: 2px 6px; font-size: 11px;")

        # 控制列
        self.controls = QWidget(self)
        self.controls.setStyleSheet("background-color: rgba(20,20,20,200);")
        ctrl = QHBoxLayout(self.controls)
        ctrl.setContentsMargins(6, 2, 6, 2)
        ctrl.setSpacing(6)

        self.mute_btn = QToolButton()
        self.mute_btn.setText("🔊")
        self.mute_btn.setToolTip("靜音 / 取消靜音")
        self.mute_btn.clicked.connect(self._on_mute_clicked)
        ctrl.addWidget(self.mute_btn)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(60)
        self.vol_slider.setMaximumWidth(160)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        ctrl.addWidget(self.vol_slider)

        ctrl.addStretch(1)

        self.zoom_btn = QToolButton()
        self.zoom_btn.setText("🔍")
        self.zoom_btn.setToolTip("大小縮放：點擊後在畫面上拖曳框選放大（或滾輪縮放）")
        self.zoom_btn.setCheckable(True)
        self.zoom_btn.toggled.connect(self._on_zoom_toggled)
        ctrl.addWidget(self.zoom_btn)

        self.reset_btn = QToolButton()
        self.reset_btn.setText("1:1")
        self.reset_btn.setToolTip("還原 100% 視角")
        self.reset_btn.clicked.connect(self.reset_zoom)
        ctrl.addWidget(self.reset_btn)

        self.fs_btn = QToolButton()
        self.fs_btn.setText("🖥️")
        self.fs_btn.setToolTip("全螢幕放大所選")
        self.fs_btn.clicked.connect(lambda: self.request_fullscreen.emit(self))
        ctrl.addWidget(self.fs_btn)

        outer.addWidget(self.video_host, 1)
        outer.addWidget(self.controls, 0)

        # 事件過濾：讓 video_host 的滑鼠事件回到本 widget 處理
        self.video_host.installEventFilter(self)
        self.setMouseTracking(True)

        self._title_timer = QTimer(self)

    # ------------------------------------------------------------------ #
    def start_player(self) -> None:
        """建立 mpv 實例並嵌入此 widget 的原生子視窗。"""
        if self.player is not None or self.mpv_module is None:
            return
        wid = int(self.video_host.winId())
        self.player = self.mpv_module.MPV(
            wid=str(wid),
            vo="gpu",
            hwdec="auto-safe",
            keep_open="no",
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            mute=True,
            volume=self._last_volume,
            cache="yes",
            demuxer_max_bytes="50MiB",
            demuxer_readahead_secs=10,
            rtsp_transport="tcp",
            network_timeout=10,
            log_handler=None,
        )
        # 自動重連：串流結束時稍後重播
        try:
            self.player.event_callback("end-file")(self._on_end_file)
        except Exception:  # noqa: BLE001
            pass
        self._play()

    def _play(self) -> None:
        if self.player is None:
            return
        try:
            self.player.play(self.camera.rtsp_url)
            debug_print(f"播放 Cam {self.camera.zero_padded_id}: {mask_url(self.camera.rtsp_url)}")
        except Exception as exc:  # noqa: BLE001
            debug_print(f"[ERROR] 播放失敗 Cam {self.camera.zero_padded_id}: {exc}")

    def _on_end_file(self, *_args) -> None:
        # 串流中斷 / 結束 -> 3 秒後自動重連
        if self.player is None:
            return
        QTimer.singleShot(3000, self._reconnect)

    def _reconnect(self) -> None:
        if self.player is None:
            return
        debug_print(f"重新連線 Cam {self.camera.zero_padded_id} ...")
        self._play()

    # ------------------------------------------------------------------ #
    def set_active_audio(self, active: bool) -> None:
        """設定是否為目前獨佔發聲的畫面（綠框）。"""
        self.active_audio = active
        if active:
            self.setStyleSheet("background-color: #000000; border: 3px solid #22ff22;")
            if self.player is not None:
                try:
                    self.player.mute = False
                    self.player.volume = self._last_volume
                except Exception:  # noqa: BLE001
                    pass
            self.mute_btn.setText("🔊")
        else:
            self.setStyleSheet("background-color: #000000;")
            if self.player is not None:
                try:
                    self.player.mute = True
                except Exception:  # noqa: BLE001
                    pass
            self.mute_btn.setText("🔇")

    def _on_mute_clicked(self) -> None:
        if self.player is None:
            return
        # 靜音鈕與音量棒互動：從靜音切回，音量還原到上次值
        try:
            currently_muted = bool(self.player.mute)
        except Exception:  # noqa: BLE001
            currently_muted = True
        if currently_muted:
            restore = self._last_volume if self._last_volume > 0 else 60
            # 明確取消靜音並套用音量（即使 slider 值未變也能生效）
            try:
                self.player.mute = False
                self.player.volume = restore
            except Exception:  # noqa: BLE001
                pass
            if self.vol_slider.value() != restore:
                self.vol_slider.setValue(restore)
            self.mute_btn.setText("🔊")
        else:
            self.vol_slider.setValue(0)

    def _on_volume_changed(self, value: int) -> None:
        if self.player is not None:
            try:
                self.player.volume = value
                self.player.mute = (value == 0)
            except Exception:  # noqa: BLE001
                pass
        if value > 0:
            self._last_volume = value
        self.mute_btn.setText("🔇" if value == 0 else "🔊")

    # ------------------------------------------------------------------ #
    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if self.player is None:
            return
        try:
            self.player.zoom = self._zoom
            self.player["video-pan-x"] = self._pan_x
            self.player["video-pan-y"] = self._pan_y
        except Exception:  # noqa: BLE001
            pass

    def zoom_at(self, cx: int, cy: int, factor: float) -> None:
        """以 (cx, cy)（相對本 widget 的像素）為中心縮放。"""
        w = max(1, self.video_host.width())
        h = max(1, self.video_host.height())
        nx = (cx / w) - 0.5
        ny = (cy / h) - 0.5
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom * factor))
        # 維持游標相對位置
        self._pan_x = (self._pan_x - nx) * (self._zoom / new_zoom) + nx
        self._pan_y = (self._pan_y - ny) * (self._zoom / new_zoom) + ny
        self._zoom = new_zoom
        if self._zoom <= 1.001:
            self._zoom = 1.0
            self._pan_x = 0.0
            self._pan_y = 0.0
        self._apply_zoom()

    def zoom_to_rect(self, rect: QRect) -> None:
        """把 rect（相對 video_host）放大填滿。"""
        w = max(1, self.video_host.width())
        h = max(1, self.video_host.height())
        if rect.width() < 8 or rect.height() < 8:
            return
        cx = (rect.x() + rect.width() / 2) / w - 0.5
        cy = (rect.y() + rect.height() / 2) / h - 0.5
        factor_w = w / rect.width()
        factor_h = h / rect.height()
        factor = min(factor_w, factor_h)
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom * factor))
        # 以游標實際位置換算 pan（mpv 的 pan 以畫面寬高為單位）
        self._pan_x = -cx * new_zoom
        self._pan_y = -cy * new_zoom
        self._zoom = new_zoom
        self._apply_zoom()

    def _on_zoom_toggled(self, checked: bool) -> None:
        # 開啟縮放模式：提示使用者拖曳框選或滾輪
        if checked:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.title_label.setText(
                f"{self.camera.zero_padded_id} ? 框選放大中（右鍵還原）")
            self.title_label.show()
            self.title_label.raise_()
            self._title_timer.singleShot(4000, self._restore_title)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._restore_title()

    def _restore_title(self) -> None:
        self.title_label.setText(f"{self.camera.zero_padded_id} ? {self.camera.cam_name}")

    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt
        super().resizeEvent(event)
        ch = self.controls.sizeHint().height()
        self.controls.setGeometry(0, self.height() - ch, self.width(), ch)
        self.video_host.setGeometry(0, 0, self.width(), self.height() - ch)
        self.title_label.move(8, 8)
        self.title_label.adjustSize()
        self.title_label.raise_()
        self.controls.raise_()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt
        # 把 video_host 的滑鼠事件轉給本 widget 處理
        et = event.type()
        if obj is self.video_host and et in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.Wheel,
        ):
            self.mouseHandle(event, offset=self.video_host.pos())
            return True
        return super().eventFilter(obj, event)

    def mouseHandle(self, event, offset: QPoint = None) -> None:
        """統一處理滑鼠（本 widget 與 video_host 共用）。"""
        off = offset or QPoint(0, 0)
        et = event.type()
        if et == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self.request_audio.emit(self)
            return

        if et == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = event.pos()
                self._drag_cur = event.pos()
                self._selecting = True
                self.update()
            elif event.button() == Qt.MouseButton.RightButton:
                self.reset_zoom()
            return

        if et == QEvent.Type.MouseMove:
            if self._selecting and self._drag_start is not None:
                self._drag_cur = event.pos()
                self.update()
            return

        if et == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.LeftButton and self._selecting:
                self._selecting = False
                if self._drag_start is not None:
                    rect = QRect(self._drag_start, event.pos()).normalized()
                    if rect.width() > 10 and rect.height() > 10:
                        self.zoom_to_rect(rect)
                self._drag_start = None
                self._drag_cur = None
                self.update()
            return

        if et == QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else (1 / 1.15)
            self.zoom_at(event.position().x(), event.position().y(), factor)
            return

    # 對本 widget 本身的事件
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.request_audio.emit(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.mouseHandle(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self.mouseHandle(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.mouseHandle(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        self.mouseHandle(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._selecting and self._drag_start is not None and self._drag_cur is not None:
            painter = QPainter(self)
            painter.setPen(QPen(QColor(30, 200, 255), 2))
            painter.setBrush(QColor(30, 200, 255, 40))
            painter.drawRect(QRect(self._drag_start, self._drag_cur).normalized())
            painter.end()

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        if self.player is not None:
            try:
                self.player.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.player = None


# =============================================================================
# 全螢幕懸浮控制面板 (HUD)
# =============================================================================
class FullscreenHUD(QFrame):
    """全螢幕時上方中央掉下的半透明控制面板。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "FullscreenHUD { background-color: rgba(20,20,20,200); "
            "border-radius: 12px; }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self.label = QLabel("全螢幕")
        self.label.setStyleSheet("color: #d0e0ff; font-size: 12px;")
        layout.addWidget(self.label)

        self.mute_btn = QPushButton("🔊 聲音開關")
        self.mute_btn.setStyleSheet("color: #e0e0e0;")
        layout.addWidget(self.mute_btn)

        self.reset_btn = QPushButton("1:1 還原")
        self.reset_btn.setStyleSheet("color: #e0e0e0;")
        layout.addWidget(self.reset_btn)

        self.exit_btn = QPushButton("✖ 退出全螢幕")
        self.exit_btn.setStyleSheet("color: #ff8080;")
        layout.addWidget(self.exit_btn)


# =============================================================================
# 主視窗
# =============================================================================
class ViewerWindow(QMainWindow):
    """主監控視窗：左側窄選單（1/2/4/6）+ 網格播放 + 聲音互鎖 + 全螢幕 HUD。"""

    def __init__(self, cameras: list, mpv_module) -> None:
        super().__init__()
        self.cameras = cameras
        self.mpv_module = mpv_module
        self.panes = []
        self.layout_count = 1
        self.active_pane = None
        self._fullscreen_pane = None

        self.setWindowTitle(f"{APP_NAME}")
        self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)
        self.setMinimumSize(DEFAULT_WIDTH, DEFAULT_HEIGHT)
        self.setStyleSheet("background-color: #0a0a0a;")

        # ---- 選單列（minimize / maximize / full screen）----
        menubar = self.menuBar()
        view_menu = menubar.addMenu("檢視 &View")
        act_min = view_menu.addAction("最小化 Minimize")
        act_min.triggered.connect(self.showMinimized)
        act_max = view_menu.addAction("最大化 Maximize")
        act_max.triggered.connect(self._toggle_maximize)
        act_fs = view_menu.addAction("全螢幕 Full Screen (F11)")
        act_fs.triggered.connect(lambda: self._enter_fullscreen(self.active_pane))

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 左側窄選單 ----
        sidebar = QWidget()
        sidebar.setFixedWidth(64)
        sidebar.setStyleSheet("background-color: #141414;")
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(6, 10, 6, 10)
        side_lay.setSpacing(10)

        self.layout_buttons = {}
        icons = {1: "▶", 2: "▦", 4: "⊞", 6: "⬢"}
        for choice in LAYOUT_CHOICES:
            btn = QToolButton()
            btn.setText(f"{icons[choice]}\n{choice}")
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setCheckable(True)
            btn.setFixedSize(52, 52)
            btn.setStyleSheet(
                "QToolButton { color:#cfd6e0; font-size:13px; border-radius:8px; }"
                "QToolButton:checked { background-color:#22436e; color:#ffffff; }")
            btn.clicked.connect(lambda _=False, c=choice: self.set_layout_count(c))
            side_lay.addWidget(btn)
            self.layout_buttons[choice] = btn
        side_lay.addStretch(1)
        root.addWidget(sidebar)

        # ---- 網格區 ----
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setSpacing(4)
        root.addWidget(self.grid_host, 1)

        self.setCentralWidget(central)

        # 先建立所有 videopane（依 config 數量），啟用前 N 個
        for camera in self.cameras:
            pane = VideoPane(camera, self.mpv_module)
            pane.request_audio.connect(self._on_request_audio)
            pane.request_fullscreen.connect(self._enter_fullscreen)
            self.panes.append(pane)

        self.set_layout_count(1)
        self.layout_buttons[1].setChecked(True)

    # ------------------------------------------------------------------ #
    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ------------------------------------------------------------------ #
    def set_layout_count(self, count: int) -> None:
        """切換主畫面播放路數（1/2/4/6）。"""
        if count not in LAYOUT_CHOICES:
            return
        self.layout_count = count
        if count in self.layout_buttons:
            self.layout_buttons[count].setChecked(True)

        # 清空網格（不銷毀 pane）：先從版面移除，再隱藏，
        # 但所有 pane 仍以 grid_host 為父物件，避免被提升成獨立視窗而保有舊尺寸。
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(self.grid_host)

        # 清空前一版面的所有 row/column stretch，避免舊的 3 欄 stretch 殘留
        # 導致切回 2 路時畫面仍被壓成 3 欄大小。
        prev_cols = self.grid.columnCount()
        prev_rows = self.grid.rowCount()
        for c in range(max(prev_cols, MAX_CAMERAS)):
            self.grid.setColumnStretch(c, 0)
            self.grid.setColumnMinimumWidth(c, 0)
        for r in range(max(prev_rows, MAX_CAMERAS)):
            self.grid.setRowStretch(r, 0)
            self.grid.setRowMinimumHeight(r, 0)

        active_panes = self.panes[:count]

        columns = 1 if count == 1 else (2 if count in (2, 4) else 3)
        for idx, pane in enumerate(active_panes):
            row, col = divmod(idx, columns)
            self.grid.addWidget(pane, row, col)
            pane.show()
            pane.start_player()

        # 只對本次版面實際使用到的欄/列設定 stretch=1
        for c in range(columns):
            self.grid.setColumnStretch(c, 1)
        rows = (len(active_panes) + columns - 1) // columns
        for r in range(rows):
            self.grid.setRowStretch(r, 1)

        # 明確隱藏未使用的 pane，並強制版面立即重算（切回較少路數時才會還原大小）
        for pane in self.panes[count:]:
            pane.hide()
        self.grid.invalidate()
        self.grid.activate()
        self.grid_host.updateGeometry()
        self.grid_host.update()

        # 音訊互鎖
        if count == 1 and active_panes:
            self._set_active(active_panes[0])
        else:
            self._mute_all()
            self.active_pane = None

    # ------------------------------------------------------------------ #
    def _mute_all(self) -> None:
        for pane in self.panes:
            pane.set_active_audio(False)
        self.active_pane = None

    def _set_active(self, pane: VideoPane) -> None:
        """獨佔啟用某畫面聲音（綠框），其餘靜音。"""
        for p in self.panes:
            p.set_active_audio(p is pane)
        self.active_pane = pane

    def _on_request_audio(self, pane: VideoPane) -> None:
        # 左鍵連點兩下：獨佔開啟該畫面聲音
        if self.active_pane is pane:
            # 再次連點可切換開關（靜音）
            self._mute_all()
        else:
            self._set_active(pane)

    # ------------------------------------------------------------------ #
    def _enter_fullscreen(self, pane) -> None:
        """把（有聲音的）畫面放大至全螢幕，並顯示 HUD。"""
        if self._fullscreen_pane is not None:
            return
        target = pane if isinstance(pane, VideoPane) else self.active_pane
        if target is None:
            target = self.panes[0] if self.panes else None
        if target is None:
            return

        # 確保此畫面有聲音且為亮綠框
        self._set_active(target)
        self._fullscreen_pane = target

        # 記憶原父物件，移到全螢幕專用容器
        self._fs_original_parent = target.parent()

        # 建立全螢幕覆蓋層
        self.fs_overlay = QWidget(self)
        self.fs_overlay.setStyleSheet("background-color: #000000;")
        fs_layout = QVBoxLayout(self.fs_overlay)
        fs_layout.setContentsMargins(0, 0, 0, 0)
        fs_layout.setSpacing(0)
        target.setParent(self.fs_overlay)
        target.show()
        fs_layout.addWidget(target)
        self.fs_overlay.setGeometry(self.rect())
        self.fs_overlay.raise_()
        self.fs_overlay.show()

        # HUD
        self.hud = FullscreenHUD(self.fs_overlay)
        self.hud.label.setText(f"全螢幕 - {target.camera.cam_name}（左鍵框選 / 右鍵還原）")
        self.hud.mute_btn.clicked.connect(lambda: self._on_request_audio(target))
        self.hud.reset_btn.clicked.connect(target.reset_zoom)
        self.hud.exit_btn.clicked.connect(self._exit_fullscreen)
        self._position_hud()

        # 進入真全螢幕
        self.showFullScreen()

    def _position_hud(self) -> None:
        if not hasattr(self, "hud") or self.hud is None:
            return
        self.hud.adjustSize()
        x = (self.fs_overlay.width() - self.hud.width()) // 2
        self.hud.move(max(0, x), 10)
        self.hud.raise_()

    def _exit_fullscreen(self) -> None:
        if self._fullscreen_pane is None:
            return
        target = self._fullscreen_pane
        # 把畫面移回原網格
        if hasattr(self, "hud") and self.hud is not None:
            self.hud.hide()
            self.hud.deleteLater()
            self.hud = None
        # 重新套用目前版面（會把 pane 放回 grid）
        target.setParent(None)
        self.fs_overlay.hide()
        self.fs_overlay.deleteLater()
        self.fs_overlay = None
        self._fullscreen_pane = None
        self.showNormal()
        self.set_layout_count(self.layout_count)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fullscreen_pane is not None and getattr(self, "fs_overlay", None) is not None:
            self.fs_overlay.setGeometry(self.rect())
            self._position_hud()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_F11:
            if self._fullscreen_pane is not None:
                self._exit_fullscreen()
            else:
                self._enter_fullscreen(self.active_pane)
        elif event.key() == Qt.Key.Key_Escape and self._fullscreen_pane is not None:
            self._exit_fullscreen()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802
        for pane in self.panes:
            pane.stop()
        super().closeEvent(event)


# =============================================================================
# 主程式
# =============================================================================
def _script_dir(argv: list) -> str:
    path = os.path.abspath(argv[0]) if argv else os.path.abspath(__file__)
    return os.path.dirname(path)


def _ensure_config_gui(config_path: str) -> list:
    """確保 viewer_config.json 存在並回傳 cameras；不存在則彈框輸入。"""
    if os.path.isfile(config_path):
        try:
            cams = load_cameras(config_path)
            if cams:
                return cams
        except Exception as exc:  # noqa: BLE001
            debug_print(f"[WARN] 讀取 viewer_config.json 失敗: {exc}")

    dialog = RtspInputDialog()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return []
    urls = dialog.urls()
    if not urls:
        return []
    try:
        write_config(config_path, urls)
        debug_print(f"已建立 viewer_config.json: {config_path} ({len(urls)} 路)")
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, "寫入失敗", f"無法寫入 viewer_config.json：\n{exc}")
        return []
    return load_cameras(config_path)


def main(argv: list = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    script_dir = _script_dir(argv)
    config_path = os.path.join(script_dir, CONFIG_FILE_NAME)

    app = QApplication(argv)

    # 載入 python-mpv（需 libmpv）
    mpv_module, mpv_err = _try_import_mpv(script_dir)
    if mpv_module is None:
        QMessageBox.critical(
            None,
            "缺少 libmpv（python-mpv）",
            "無法載入 python-mpv 播放引擎。\n\n"
            "請確認已安裝 python-mpv，且系統可找到 libmpv：\n"
            "  pip install python-mpv\n\n"
            "Windows 需下載 mpv 並將 mpv-2.dll / libmpv-2.dll 所在資料夾\n"
            "加入系統 PATH，或放到本程式同一資料夾。\n\n"
            "下載：https://mpv.io/installation/\n\n"
            f"詳細錯誤：\n{mpv_err}",
        )
        return 1

    debug_print(f"python-mpv 已載入，mpv 版本: {getattr(mpv_module, 'MPV', None)}")

    cameras = _ensure_config_gui(config_path)
    if not cameras:
        debug_print("無可播放的攝影機設定，結束。")
        return 0

    debug_print(f"攝影機數量: {len(cameras)}")
    for cam in cameras:
        debug_print(f"  Cam {cam.zero_padded_id} | {cam.cam_name} | {mask_url(cam.rtsp_url)}")

    window = ViewerWindow(cameras, mpv_module)
    window.show()
    exit_code = app.exec()
    debug_print("播放器結束")
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
