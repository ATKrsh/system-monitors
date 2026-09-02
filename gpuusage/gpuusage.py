import sys
import os
import time
import math
import subprocess
import collections
import platform
import json
import struct
import io
import tempfile
import psutil
from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QAction, QActionGroup, QWidgetAction, QSlider, QHBoxLayout, QLabel
from PyQt5.QtCore import Qt, QPoint, QPointF, QRectF, QTimer, pyqtSignal, QThread, QUrl
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient, QIcon, QPixmap, QPainterPath
from PyQt5.QtMultimedia import QSoundEffect

_IS_WINDOWS = platform.system() == "Windows"
if _IS_WINDOWS:
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

APP_NAME = "GPUUsageMonitor"

def set_start_with_windows(enable):
    if not _IS_WINDOWS or winreg is None: return False
    exe_path = sys.executable
    if not exe_path.endswith('.exe'):
        script_path = os.path.abspath(sys.argv[0])
        exe_path = f'"{exe_path.replace("python.exe","pythonw.exe")}" "{script_path}"'
    else:
        exe_path = f'"{exe_path}"'
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enable: winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError: pass
        winreg.CloseKey(key); return True
    except Exception as e:
        print(f"Error: {e}"); return False

def is_start_with_windows_enabled():
    if not _IS_WINDOWS or winreg is None: return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME); winreg.CloseKey(key); return True
    except: return False

def get_config_path():
    return os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), f"{APP_NAME}_config.json")

def load_config():
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, 'r') as f: return json.load(f)
        except: pass
    return {}

def save_config(config):
    try:
        with open(get_config_path(), 'w') as f: json.dump(config, f)
    except: pass

def generate_sine_wave_wav(frequency, duration_ms, sample_rate=22050):
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    buf = io.BytesIO()
    import wave
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate)
        for i in range(num_samples):
            t = float(i) / sample_rate
            wav.writeframes(struct.pack('<h', int(32767.0 * math.sin(2.0*math.pi*frequency*t))))
    buf.seek(0); return buf.read()

def create_temp_wav(name, freq, duration_ms):
    data = generate_sine_wave_wav(freq, duration_ms)
    path = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_{name}.wav")
    try:
        with open(path, 'wb') as f: f.write(data)
    except: pass
    return path


class GPUUsageWorker(QThread):
    usage_updated = pyqtSignal(float, float, float, str, str, str, float)  # dgpu_u, igpu_u, total_u, dgpu_name, igpu_name, top_proc_name, top_proc_pct

    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self.dgpu_name = "NVIDIA GeForce RTX 3050"
        self.igpu_name = "AMD Radeon(TM) Graphics"
        self._has_nvml = False
        self._nvml_dev = None
        self._nvml = None

        try:
            import ctypes
            self._nvml = ctypes.CDLL("nvml.dll")
            if self._nvml.nvmlInit_v2() == 0:
                self._nvml_dev = ctypes.c_void_p()
                if self._nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._nvml_dev)) == 0:
                    name_buf = ctypes.create_string_buffer(64)
                    if self._nvml.nvmlDeviceGetName(self._nvml_dev, name_buf, 64) == 0:
                        self.dgpu_name = name_buf.value.decode("utf-8", errors="ignore")
                    self._has_nvml = True
        except Exception:
            self._has_nvml = False

        # Fast PDH counter for integrated GPU and process utilization
        self._has_pdh = False
        self._h_query = None
        self._h_gpu_counter = None
        try:
            import ctypes
            from ctypes import wintypes
            self.pdh = ctypes.windll.pdh
            self._h_query = wintypes.HANDLE()
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._h_query)) == 0:
                self._h_gpu_counter = wintypes.HANDLE()
                if self.pdh.PdhAddCounterW(
                    self._h_query, "\\GPU Engine(*)\\Utilization Percentage", 0, ctypes.byref(self._h_gpu_counter)
                ) == 0:
                    self.pdh.PdhCollectQueryData(self._h_query)
                    self._has_pdh = True
        except Exception:
            self._has_pdh = False

        self._last_dgpu_util = 0.0
        self._last_igpu_util = 0.0
        self._last_total_util = 0.0
        self._top_proc_name = "Idle"
        self._top_proc_pct = 0.0

    def run(self):
        while self._running:
            dgpu_u, igpu_u, total_u = self._query_gpu_util()
            self.usage_updated.emit(dgpu_u, igpu_u, total_u, self.dgpu_name, self.igpu_name, self._top_proc_name, self._top_proc_pct)
            self.msleep(self.interval_ms)

    def _query_gpu_util(self) -> tuple:
        # 1. Dedicated GPU via NVML
        if self._has_nvml and self._nvml_dev:
            try:
                import ctypes
                class nvmlUtil(ctypes.Structure):
                    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]
                util_val = nvmlUtil()
                if self._nvml.nvmlDeviceGetUtilizationRates(self._nvml_dev, ctypes.byref(util_val)) == 0:
                    self._last_dgpu_util = float(util_val.gpu)
            except Exception:
                pass
        else:
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=0.5, creationflags=0x08000000
                )
                if result.returncode == 0:
                    val = result.stdout.strip()
                    if val:
                        self._last_dgpu_util = float(val)
            except Exception:
                pass

        # 2. Integrated GPU & Top Process via PDH Engine Counters
        total_pdh = 0.0
        gpu_by_pid = {}
        if self._has_pdh and self._h_query:
            try:
                import ctypes
                from ctypes import wintypes
                res = self.pdh.PdhCollectQueryData(self._h_query)
                if res == 0:
                    buf_size = wintypes.DWORD(0)
                    item_count = wintypes.DWORD(0)
                    status = self.pdh.PdhGetFormattedCounterArrayW(
                        self._h_gpu_counter, 0x00000200, ctypes.byref(buf_size), ctypes.byref(item_count), None
                    )
                    if buf_size.value > 0:
                        buf = ctypes.create_string_buffer(buf_size.value)
                        status = self.pdh.PdhGetFormattedCounterArrayW(
                            self._h_gpu_counter, 0x00000200, ctypes.byref(buf_size), ctypes.byref(item_count), ctypes.byref(buf)
                        )
                        if status == 0 and item_count.value > 0:
                            class PDH_DOUBLE(ctypes.Structure):
                                _fields_ = [("CStatus", wintypes.DWORD), ("dummy", wintypes.DWORD), ("doubleValue", ctypes.c_double)]
                            class PDH_ITEM(ctypes.Structure):
                                _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PDH_DOUBLE)]
                            items = ctypes.cast(buf, ctypes.POINTER(PDH_ITEM))
                            for i in range(item_count.value):
                                v = items[i].FmtValue.doubleValue
                                name = items[i].szName
                                if v > 0:
                                    total_pdh += v
                                    if v > 0.5 and "pid_" in name:
                                        try:
                                            pid = int(name.split("pid_")[1].split("_")[0])
                                            if pid > 0 and pid != 4:
                                                gpu_by_pid[pid] = gpu_by_pid.get(pid, 0.0) + v
                                        except Exception:
                                            pass
            except Exception:
                pass

        if gpu_by_pid:
            top_pid, top_u = max(gpu_by_pid.items(), key=lambda x: x[1])
            try:
                self._top_proc_name = psutil.Process(top_pid).name()
            except Exception:
                self._top_proc_name = f"PID {top_pid}"
            self._top_proc_pct = round(top_u, 1)
        elif self._last_dgpu_util > 0:
            self._top_proc_pct = round(self._last_dgpu_util, 1)
        else:
            self._top_proc_name = "Idle"
            self._top_proc_pct = 0.0

        self._last_total_util = max(self._last_dgpu_util, min(100.0, total_pdh))
        self._last_igpu_util = max(0.0, min(100.0, total_pdh - self._last_dgpu_util))

        return self._last_dgpu_util, self._last_igpu_util, self._last_total_util

    def stop(self):
        self._running = False
        if self._has_pdh and self._h_query:
            try:
                self.pdh.PdhCloseQuery(self._h_query)
            except Exception:
                pass
            self._h_query = None



class VolumeSliderAction(QWidgetAction):
    def __init__(self, parent, initial_volume=50, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget(parent)
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4); lay.setSpacing(8)
        self.lbl = QLabel("Volume:", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(0, 100); self.slider.setValue(initial_volume); self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#aa44ff;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#aa44ff;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class SizeSliderAction(QWidgetAction):
    def __init__(self, parent, initial_size=60, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget(parent)
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4); lay.setSpacing(8)
        self.lbl = QLabel("Size:  ", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(40, 120); self.slider.setValue(initial_size); self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#aa44ff;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#aa44ff;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class OpacitySliderAction(QWidgetAction):
    def __init__(self, parent, initial_opacity=50, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget(parent)
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4); lay.setSpacing(8)
        self.lbl = QLabel("Opacity:", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(10, 100); self.slider.setValue(initial_opacity); self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#aa44ff;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#aa44ff;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class GPUUsageWidget(QWidget):
    GRAPH_LEN = 16  # number of history points scrolled in mini-graph

    def __init__(self):
        super().__init__()
        config = load_config()
        self.is_locked = config.get("is_locked", False)
        self.click_through = config.get("click_through", False)
        self.display_mode = config.get("display_mode", 3)
        self.transition_mode = config.get("transition_mode", "smooth")
        self.sound_enabled = config.get("sound_enabled", True)
        self.volume = config.get("volume", 50)
        self.widget_size = config.get("widget_size", 60)
        self.base_opacity = config.get("base_opacity", 50)
        self.is_monitoring = True

        if not config.get("startup_registered", False):
            set_start_with_windows(True)
            config["startup_registered"] = True; save_config(config)

        self.alert_wav_path = create_temp_wav("alert", 800, 160)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        self.sound_alert.setVolume(self.volume / 100.0)

        self.raw_dgpu_usage = 0.0
        self.raw_igpu_usage = 0.0
        self.raw_total_usage = 0.0
        self.raw_usage = 0.0
        self.displayed_usage = 0.0
        self.dgpu_name = "NVIDIA GeForce RTX 3050"
        self.igpu_name = "AMD Radeon(TM) Graphics"
        self.gpu_focus = config.get("gpu_focus", "total")  # "total", "dgpu", "igpu"
        self.last_beep_time = 0.0
        self.beep_cooldown = 6.0

        # Rolling history for mini-graph
        self._history = collections.deque([0.0] * self.GRAPH_LEN, maxlen=self.GRAPH_LEN)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(self.widget_size, self.widget_size)
        self.setWindowTitle("GPU Usage Monitor")
        self._restore_position(config)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._update_ui_state)
        self.ui_timer.start(16)

        self.monitor_worker = GPUUsageWorker(interval_ms=50)
        self.monitor_worker.usage_updated.connect(self._on_usage_updated)
        if self.is_monitoring:
            self.monitor_worker.start()

        self._setup_tray()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        pix = QPixmap(16, 16); pix.fill(Qt.transparent)
        p = QPainter(pix); p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(0, 230, 100))); p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12); p.end()
        self.tray.setIcon(QIcon(pix))
        self.tray.setToolTip("GPU Usage Monitor")

        self.menu = QMenu()
        self.act_start = QAction("Start", self); self.act_start.triggered.connect(self.start_monitoring); self.act_start.setEnabled(False)
        self.act_stop = QAction("Stop", self); self.act_stop.triggered.connect(self.stop_monitoring)
        self.act_lock = QAction("Lock Position", self, checkable=True); self.act_lock.setChecked(self.is_locked); self.act_lock.triggered.connect(self.toggle_lock)
        self.act_click = QAction("Click Through", self, checkable=True); self.act_click.setChecked(self.click_through); self.act_click.triggered.connect(self.toggle_click_through)

        gpu_menu = QMenu("GPU Focus", self); gg = QActionGroup(self)
        self.act_gpu_total = QAction("Both GPUs (Combined Total)", self, checkable=True); self.act_gpu_total.setChecked(self.gpu_focus == "total"); self.act_gpu_total.triggered.connect(lambda: self.change_gpu_focus("total"))
        self.act_gpu_dgpu = QAction("Dedicated GPU (dGPU)", self, checkable=True); self.act_gpu_dgpu.setChecked(self.gpu_focus == "dgpu"); self.act_gpu_dgpu.triggered.connect(lambda: self.change_gpu_focus("dgpu"))
        self.act_gpu_igpu = QAction("Integrated GPU (iGPU)", self, checkable=True); self.act_gpu_igpu.setChecked(self.gpu_focus == "igpu"); self.act_gpu_igpu.triggered.connect(lambda: self.change_gpu_focus("igpu"))
        gg.addAction(self.act_gpu_total); gg.addAction(self.act_gpu_dgpu); gg.addAction(self.act_gpu_igpu)
        gpu_menu.addAction(self.act_gpu_total); gpu_menu.addAction(self.act_gpu_dgpu); gpu_menu.addAction(self.act_gpu_igpu)

        mode_menu = QMenu("Display Mode", self); mg = QActionGroup(self)
        for i, label in [(1,"Flat Dot"),(2,"Activity Glow"),(3,"Animated Icon")]:
            a = QAction(f"Mode {i}: {label}", self, checkable=True)
            a.setChecked(self.display_mode == i)
            a.triggered.connect(lambda _, x=i: self.change_mode(x))
            mg.addAction(a); mode_menu.addAction(a)
            setattr(self, f"act_mode{i}", a)

        trans_menu = QMenu("Transition Style", self); tg = QActionGroup(self)
        self.act_trans_smooth = QAction("Smooth Gradient", self, checkable=True); self.act_trans_smooth.setChecked(self.transition_mode=="smooth"); self.act_trans_smooth.triggered.connect(lambda: self.change_transition("smooth"))
        self.act_trans_snappy = QAction("Snappy Snap", self, checkable=True); self.act_trans_snappy.setChecked(self.transition_mode=="snappy"); self.act_trans_snappy.triggered.connect(lambda: self.change_transition("snappy"))
        tg.addAction(self.act_trans_smooth); tg.addAction(self.act_trans_snappy)
        trans_menu.addAction(self.act_trans_smooth); trans_menu.addAction(self.act_trans_snappy)

        self.act_sound = QAction("High Usage Alarm On", self, checkable=True); self.act_sound.setChecked(self.sound_enabled); self.act_sound.triggered.connect(self.toggle_sound)
        self.vol_action = VolumeSliderAction(self, self.volume, self.change_volume)
        self.size_action = SizeSliderAction(self, self.widget_size, self.change_widget_size)
        self.opacity_action = OpacitySliderAction(self, self.base_opacity, self.change_base_opacity)
        self.act_startup = QAction("Start with Windows", self, checkable=True); self.act_startup.setChecked(is_start_with_windows_enabled()); self.act_startup.triggered.connect(self.toggle_startup)
        act_exit = QAction("Exit", self); act_exit.triggered.connect(self.exit_app)
 
        for a in [self.act_start, self.act_stop, None, gpu_menu, mode_menu, trans_menu, self.act_lock, self.act_click, None, self.act_sound, self.vol_action, self.size_action, self.opacity_action, self.act_startup, None, act_exit]:
            if a is None: self.menu.addSeparator()
            elif isinstance(a, QMenu): self.menu.addMenu(a)
            elif isinstance(a, QWidgetAction): self.menu.addAction(a)
            else: self.menu.addAction(a)
 
        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def change_gpu_focus(self, focus):
        self.gpu_focus = focus
        config = load_config()
        config["gpu_focus"] = focus
        save_config(config)

    def contextMenuEvent(self, event): self.menu.popup(event.globalPos())

    def _on_usage_updated(self, dgpu_u, igpu_u, total_u, dgpu_n, igpu_n, top_name="Idle", top_pct=0.0):
        self.raw_dgpu_usage = dgpu_u
        self.raw_igpu_usage = igpu_u
        self.raw_total_usage = total_u
        self.dgpu_name = dgpu_n
        self.igpu_name = igpu_n
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct

        if self.gpu_focus == "dgpu":
            self.raw_usage = self.raw_dgpu_usage
        elif self.gpu_focus == "igpu":
            self.raw_usage = self.raw_igpu_usage
        else:
            self.raw_usage = self.raw_total_usage

        self._history.append(self.raw_usage)
        if self.sound_enabled and self.raw_usage >= 95.0:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now; self.sound_alert.play()

    def _get_color_for_percentage(self, pct, mode):
        if mode == "snappy":
            if pct < 40.0:   return QColor(0, 230, 100)
            elif pct < 75.0: return QColor(255, 170, 0)
            else:            return QColor(255, 45, 45)
        t = pct / 100.0
        if t < 0.5:
            s = t * 2.0
            return QColor(int(240*s), int(230+(180-230)*s), int(100-100*s))
        s = (t - 0.5) * 2.0
        return QColor(int(240+15*s), int(180+(45-180)*s), int(45*s))

    def _update_ui_state(self):
        if not self.is_monitoring: return

        if self.transition_mode == "smooth":
            self.displayed_usage += (self.raw_usage - self.displayed_usage) * 0.15
        else:
            self.displayed_usage = self.raw_usage

        c = self._get_color_for_percentage(self.displayed_usage, self.transition_mode)
        pix = QPixmap(16, 16); pix.fill(Qt.transparent)
        p = QPainter(pix); p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(c)); p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12); p.end()
        self.tray.setIcon(QIcon(pix))
        tip_text = f"dGPU ({self.dgpu_name}): {self.raw_dgpu_usage:.0f}%\niGPU ({self.igpu_name}): {self.raw_igpu_usage:.0f}%\nTotal GPU: {self.raw_total_usage:.0f}%\nTop GPU: {getattr(self, 'top_proc_name', 'Idle')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)"
        self.tray.setToolTip(tip_text)
        self.setToolTip(tip_text)
        self.setWindowOpacity(0.5 * (self.base_opacity / 100.0))
        self.update()


    def draw_animated_icon(self, p, cx, cy, pct, core_color):
        p.save()
        # 1. Draw the grey base icon as outline
        self.draw_icon_graphics(p, cx, cy, pct, QColor(130, 135, 145), fill=False)
        # 2. Draw the active color-filled overlay with bottom-up clipping as total fill
        fill_h = (pct / 100.0) * 18.0
        clip_rect = QRectF(cx - 12.0, cy + 9.0 - fill_h, 24.0, fill_h)
        p.setClipRect(clip_rect)
        self.draw_icon_graphics(p, cx, cy, pct, core_color, fill=True)
        p.restore()

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # Mini screen and scrolling graph
        p.setPen(QPen(color, 1.0, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)

        screen_w, screen_h = 13.0, 9.0
        screen_x = cx - screen_w / 2.0
        screen_y = cy - screen_h / 2.0 - 1.0
        p.drawRoundedRect(QRectF(screen_x, screen_y, screen_w, screen_h), 1.0, 1.0)

        p.drawLine(QPointF(cx - 1.5, cy + 4.0), QPointF(cx + 1.5, cy + 4.0))
        p.drawLine(QPointF(cx, cy + screen_h/2.0 - 1.5), QPointF(cx, cy + 4.0))

        history = list(self._history)
        n = len(history)
        if n > 1:
            graph_x0 = screen_x + 1.5
            graph_x1 = screen_x + screen_w - 1.5
            graph_y_bot = screen_y + screen_h - 1.5
            graph_y_top = screen_y + 1.5
            graph_range_y = graph_y_bot - graph_y_top
            graph_range_x = graph_x1 - graph_x0

            graph_path = QPainterPath()
            for idx, val in enumerate(history):
                px = graph_x0 + (idx / (n - 1)) * graph_range_x
                py = graph_y_bot - (val / 100.0) * graph_range_y
                if idx == 0:
                    graph_path.moveTo(px, py)
                else:
                    graph_path.lineTo(px, py)

            if fill:
                area_path = QPainterPath(graph_path)
                area_path.lineTo(graph_x1, graph_y_bot)
                area_path.lineTo(graph_x0, graph_y_bot)
                area_path.closeSubpath()
                p.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 100)))
                p.setPen(Qt.NoPen)
                p.drawPath(area_path)

            graph_pen = QPen(QColor(color.red(), color.green(), color.blue(), 220), 0.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(graph_pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(graph_path)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Scale painter to support dynamic widget resizing
        scale_factor = self.widget_size / 60.0
        p.scale(scale_factor, scale_factor)

        cx, cy = 30.0, 30.0
        r_base = 12.0
        pct = self.displayed_usage
        core_color = self._get_color_for_percentage(pct, self.transition_mode)

        if self.display_mode == 2:
            glow_r = r_base + 8.0
            glow = QRadialGradient(cx, cy, glow_r)
            glow.setColorAt(0.0, QColor(core_color.red(), core_color.green(), core_color.blue(), 150))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow)); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        p.setPen(Qt.NoPen)
        if self.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
            return

        p.setBrush(QBrush(core_color))
        p.drawEllipse(QPointF(cx, cy), r_base, r_base)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        p.drawEllipse(QPointF(cx, cy), r_base - 0.5, r_base - 0.5)

        # ── Mini screen icon with scrolling GPU usage graph ──────────────
        icon_color = QColor(255, 255, 255, 210)
        p.setPen(QPen(icon_color, 1.0, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)

        # Screen body outline
        screen_w, screen_h = 13.0, 9.0
        screen_x = cx - screen_w / 2.0
        screen_y = cy - screen_h / 2.0 - 1.0
        p.drawRoundedRect(QRectF(screen_x, screen_y, screen_w, screen_h), 1.0, 1.0)

        # Stand (two small lines below screen)
        p.drawLine(QPointF(cx - 1.5, cy + 4.0), QPointF(cx + 1.5, cy + 4.0))
        p.drawLine(QPointF(cx, cy + screen_h/2.0 - 1.5), QPointF(cx, cy + 4.0))

        # Scrolling wave graph inside screen
        history = list(self._history)
        n = len(history)
        if n > 1:
            graph_x0 = screen_x + 1.5
            graph_x1 = screen_x + screen_w - 1.5
            graph_y_bot = screen_y + screen_h - 1.5
            graph_y_top = screen_y + 1.5
            graph_range_y = graph_y_bot - graph_y_top
            graph_range_x = graph_x1 - graph_x0

            graph_path = QPainterPath()
            for idx, val in enumerate(history):
                px = graph_x0 + (idx / (n - 1)) * graph_range_x
                py = graph_y_bot - (val / 100.0) * graph_range_y
                if idx == 0:
                    graph_path.moveTo(px, py)
                else:
                    graph_path.lineTo(px, py)

            graph_pen = QPen(QColor(255, 255, 255, 220), 0.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(graph_pen)
            p.drawPath(graph_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.is_locked:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft(); event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and not self.is_locked:
            self.move(event.globalPos() - self._drag_pos); event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.is_locked:
            config = load_config(); config["x"] = self.x(); config["y"] = self.y()
            save_config(config); event.accept()

    def start_monitoring(self):
        if not self.is_monitoring:
            self.is_monitoring = True; self.monitor_worker.start()
            self.act_start.setEnabled(False); self.act_stop.setEnabled(True)

    def stop_monitoring(self):
        if self.is_monitoring:
            self.is_monitoring = False; self.monitor_worker.stop(); self.monitor_worker.wait()
            self.displayed_usage = 0.0
            self.act_start.setEnabled(True); self.act_stop.setEnabled(False); self.update()

    def change_mode(self, mode):
        self.display_mode = mode
        for i in [1,2,3]: getattr(self, f"act_mode{i}").setChecked(mode==i)
        self.update(); config = load_config(); config["display_mode"] = mode; save_config(config)

    def change_transition(self, mode):
        self.transition_mode = mode
        self.act_trans_smooth.setChecked(mode=="smooth"); self.act_trans_snappy.setChecked(mode=="snappy")
        config = load_config(); config["transition_mode"] = mode; save_config(config)

    def toggle_lock(self, checked):
        self.is_locked = checked; self.act_lock.setChecked(checked)
        config = load_config(); config["is_locked"] = checked; save_config(config)

    def toggle_click_through(self, checked):
        self.click_through = checked; self.act_click.setChecked(checked)
        config = load_config(); config["click_through"] = checked; save_config(config)
        self.hide()
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.SubWindow
        if checked: flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags); self.show()

    def toggle_sound(self, checked):
        self.sound_enabled = checked; self.act_sound.setChecked(checked)
        config = load_config(); config["sound_enabled"] = checked; save_config(config)

    def change_volume(self, val):
        self.volume = val; self.sound_alert.setVolume(val / 100.0)
        config = load_config(); config["volume"] = val; save_config(config)

    def change_widget_size(self, val):
        self.widget_size = val
        self.apply_widget_size(val)
        config = load_config()
        config["widget_size"] = val
        save_config(config)

    def apply_widget_size(self, val):
        self.setFixedSize(val, val)

    def change_base_opacity(self, val):
        self.base_opacity = val
        self.apply_base_opacity(val)
        config = load_config()
        config["base_opacity"] = val
        save_config(config)

    def apply_base_opacity(self, val):
        self.update()

    def toggle_startup(self, checked):
        set_start_with_windows(checked); self.act_startup.setChecked(checked)

    def _restore_position(self, config):
        screen = QApplication.primaryScreen().geometry()
        x = config.get("x", screen.width() - self.width() - 50)
        y = config.get("y", screen.height() - self.height() - 450)
        self.move(x, y)

    def exit_app(self):
        self.monitor_worker.stop(); self.monitor_worker.wait(); QApplication.quit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app.setQuitOnLastWindowClosed(False)
    w = GPUUsageWidget()
    w.show()
    sys.exit(app.exec_())
