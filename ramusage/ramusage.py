import sys
import os
import time
import math
import random
import platform
import json
import struct
import io
import tempfile
import psutil
from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QAction, QActionGroup, QWidgetAction, QSlider, QHBoxLayout, QLabel
from PyQt5.QtCore import Qt, QPoint, QPointF, QRectF, QTimer, pyqtSignal, QThread, QUrl
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient, QIcon, QPixmap
from PyQt5.QtMultimedia import QSoundEffect

_IS_WINDOWS = platform.system() == "Windows"
if _IS_WINDOWS:
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

APP_NAME = "RAMUsageMonitor"

def set_start_with_windows(enable):
    if not _IS_WINDOWS or winreg is None:
        return False
    exe_path = sys.executable
    if not exe_path.endswith('.exe'):
        script_path = os.path.abspath(sys.argv[0])
        pythonw_path = exe_path.replace("python.exe", "pythonw.exe")
        exe_path = f'"{pythonw_path}" "{script_path}"'
    else:
        exe_path = f'"{exe_path}"'

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Error setting registry: {e}")
        return False

def is_start_with_windows_enabled():
    if not _IS_WINDOWS or winreg is None:
        return False
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

def get_config_path():
    dir_path = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(dir_path, f"{APP_NAME}_config.json")

def load_config():
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(config):
    path = get_config_path()
    try:
        with open(path, 'w') as f:
            json.dump(config, f)
    except Exception:
        pass

def generate_sine_wave_wav(frequency, duration_ms, sample_rate=22050):
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    buf = io.BytesIO()
    import wave
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(num_samples):
            t = float(i) / sample_rate
            val = int(32767.0 * math.sin(2.0 * math.pi * frequency * t))
            wav.writeframes(struct.pack('<h', val))
    buf.seek(0)
    return buf.read()

def create_temp_wav(name, freq, duration_ms):
    data = generate_sine_wave_wav(freq, duration_ms)
    temp_dir = tempfile.gettempdir()
    path = os.path.join(temp_dir, f"{APP_NAME}_{name}.wav")
    try:
        with open(path, 'wb') as f:
            f.write(data)
    except Exception:
        pass
    return path

class RAMUsageWorker(QThread):
    ram_updated = pyqtSignal(float, str, float)  # ram_pct, top_proc_name, top_proc_mem_pct

    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self._last_top_check = 0.0
        self._top_proc_name = "System"
        self._top_proc_pct = 0.0

    def run(self):
        while self._running:
            try:
                mem = psutil.virtual_memory()
                ram_pct = mem.percent
            except Exception:
                ram_pct = 0.0
            self._update_top_process()
            self.ram_updated.emit(ram_pct, self._top_proc_name, self._top_proc_pct)
            self.msleep(self.interval_ms)

    def _update_top_process(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:  # sample top proc every 250ms
            return
        self._last_top_check = now
        try:
            best_mem = 0.0
            best_name = "System"
            for p in psutil.process_iter(['name', 'memory_percent']):
                try:
                    name = p.info['name']
                    if not name or name.lower() in ('system idle process', 'idle'):
                        continue
                    m = p.info['memory_percent'] or 0.0
                    if m > best_mem:
                        best_mem = m
                        best_name = name
                except Exception:
                    pass
            self._top_proc_name = best_name
            self._top_proc_pct = round(best_mem, 1)
        except Exception:
            pass

    def stop(self):
        self._running = False


class VolumeSliderAction(QWidgetAction):
    def __init__(self, parent, initial_volume=50, callback=None):
        super().__init__(parent)
        self.callback = callback
        
        self.widget = QWidget(parent)
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        
        self.lbl = QLabel("Volume:", self.widget)
        self.lbl.setStyleSheet("color: #cccccc; font-size: 10px; font-weight: bold;")
        lay.addWidget(self.lbl)
        
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(0, 100)
        self.slider.setValue(initial_volume)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider {
                background: transparent;
            }
            QSlider::groove:horizontal {
                background: rgba(255,255,255,0.12);
                height: 4px;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #ff2d2d;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                width: 10px;
                height: 10px;
                margin-top: -3px;
                margin-bottom: -3px;
                border-radius: 5px;
            }
            QSlider::handle:horizontal:hover {
                background: #ff2d2d;
            }
        """)
        self.slider.valueChanged.connect(self._on_value_changed)
        lay.addWidget(self.slider)
        
        self.setDefaultWidget(self.widget)
        
    def _on_value_changed(self, val):
        if self.callback:
            self.callback(val)


class SizeSliderAction(QWidgetAction):
    def __init__(self, parent, initial_size=60, callback=None):
        super().__init__(parent)
        self.callback = callback
        
        self.widget = QWidget(parent)
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        
        self.lbl = QLabel("Size:  ", self.widget)
        self.lbl.setStyleSheet("color: #cccccc; font-size: 10px; font-weight: bold;")
        lay.addWidget(self.lbl)
        
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(40, 120)
        self.slider.setValue(initial_size)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider {
                background: transparent;
            }
            QSlider::groove:horizontal {
                background: rgba(255,255,255,0.12);
                height: 4px;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #ff2d2d;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                width: 10px;
                height: 10px;
                margin-top: -3px;
                margin-bottom: -3px;
                border-radius: 5px;
            }
            QSlider::handle:horizontal:hover {
                background: #ff2d2d;
            }
        """)
        self.slider.valueChanged.connect(self._on_value_changed)
        lay.addWidget(self.slider)
        
        self.setDefaultWidget(self.widget)
        
    def _on_value_changed(self, val):
        if self.callback:
            self.callback(val)


class OpacitySliderAction(QWidgetAction):
    def __init__(self, parent, initial_opacity=50, callback=None):
        super().__init__(parent)
        self.callback = callback
        
        self.widget = QWidget(parent)
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        
        self.lbl = QLabel("Opacity:", self.widget)
        self.lbl.setStyleSheet("color: #cccccc; font-size: 10px; font-weight: bold;")
        lay.addWidget(self.lbl)
        
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(10, 100)
        self.slider.setValue(initial_opacity)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider {
                background: transparent;
            }
            QSlider::groove:horizontal {
                background: rgba(255,255,255,0.12);
                height: 4px;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #ff2d2d;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                width: 10px;
                height: 10px;
                margin-top: -3px;
                margin-bottom: -3px;
                border-radius: 5px;
            }
            QSlider::handle:horizontal:hover {
                background: #ff2d2d;
            }
        """)
        self.slider.valueChanged.connect(self._on_value_changed)
        lay.addWidget(self.slider)
        
        self.setDefaultWidget(self.widget)
        
    def _on_value_changed(self, val):
        if self.callback:
            self.callback(val)


class RAMUsageWidget(QWidget):
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
            config["startup_registered"] = True
            save_config(config)

        self.alert_wav_path = create_temp_wav("alert", 700, 150)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        self.sound_alert.setVolume(self.volume / 100.0)

        self.raw_ram = 50.0
        self.displayed_ram = 50.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 10.0 # High memory is persistent, alert less frequently

        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.SubWindow
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(self.widget_size, self.widget_size)
        self.setWindowTitle("RAM Usage Monitor")

        self._restore_position(config)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._update_ui_state)
        self.ui_timer.start(16)

        self.monitor_worker = RAMUsageWorker(interval_ms=1000)
        self.monitor_worker.ram_updated.connect(self._on_ram_updated)
        if self.is_monitoring:
            self.monitor_worker.start()

        self._setup_tray()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(0, 230, 100)))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12)
        p.end()
        
        self.tray.setIcon(QIcon(pix))
        self.tray.setToolTip("RAM Usage Monitor")

        self.menu = QMenu()
        
        self.act_start = QAction("Start", self)
        self.act_start.triggered.connect(self.start_monitoring)
        self.act_start.setEnabled(False)
        
        self.act_stop = QAction("Stop", self)
        self.act_stop.triggered.connect(self.stop_monitoring)
        
        self.act_lock = QAction("Lock Position", self, checkable=True)
        self.act_lock.setChecked(self.is_locked)
        self.act_lock.triggered.connect(self.toggle_lock)
        
        self.act_click = QAction("Click Through", self, checkable=True)
        self.act_click.setChecked(self.click_through)
        self.act_click.triggered.connect(self.toggle_click_through)

        mode_menu = QMenu("Display Mode", self)
        mode_group = QActionGroup(self)
        self.act_mode1 = QAction("Mode 1: Flat Dot", self, checkable=True)
        self.act_mode1.setChecked(self.display_mode == 1)
        self.act_mode1.triggered.connect(lambda: self.change_mode(1))
        
        self.act_mode2 = QAction("Mode 2: Activity Glow", self, checkable=True)
        self.act_mode2.setChecked(self.display_mode == 2)
        self.act_mode2.triggered.connect(lambda: self.change_mode(2))
        
        self.act_mode3 = QAction("Mode 3: Animated Icon", self, checkable=True)
        self.act_mode3.setChecked(self.display_mode == 3)
        self.act_mode3.triggered.connect(lambda: self.change_mode(3))
        
        mode_group.addAction(self.act_mode1)
        mode_group.addAction(self.act_mode2)
        mode_group.addAction(self.act_mode3)
        mode_menu.addAction(self.act_mode1)
        mode_menu.addAction(self.act_mode2)
        mode_menu.addAction(self.act_mode3)

        trans_menu = QMenu("Transition Style", self)
        trans_group = QActionGroup(self)
        self.act_trans_smooth = QAction("Smooth Gradient", self, checkable=True)
        self.act_trans_smooth.setChecked(self.transition_mode == "smooth")
        self.act_trans_smooth.triggered.connect(lambda: self.change_transition("smooth"))
        
        self.act_trans_snappy = QAction("Snappy Snap", self, checkable=True)
        self.act_trans_snappy.setChecked(self.transition_mode == "snappy")
        self.act_trans_snappy.triggered.connect(lambda: self.change_transition("snappy"))
        
        trans_group.addAction(self.act_trans_smooth)
        trans_group.addAction(self.act_trans_snappy)
        trans_menu.addAction(self.act_trans_smooth)
        trans_menu.addAction(self.act_trans_snappy)

        self.act_sound = QAction("High RAM Alert On", self, checkable=True)
        self.act_sound.setChecked(self.sound_enabled)
        self.act_sound.triggered.connect(self.toggle_sound)

        self.vol_action = VolumeSliderAction(self, self.volume, self.change_volume)
        self.size_action = SizeSliderAction(self, self.widget_size, self.change_widget_size)
        self.opacity_action = OpacitySliderAction(self, self.base_opacity, self.change_base_opacity)

        self.act_startup = QAction("Start with Windows", self, checkable=True)
        self.act_startup.setChecked(is_start_with_windows_enabled())
        self.act_startup.triggered.connect(self.toggle_startup)
        
        act_exit = QAction("Exit", self)
        act_exit.triggered.connect(self.exit_app)

        self.menu.addAction(self.act_start)
        self.menu.addAction(self.act_stop)
        self.menu.addSeparator()
        self.menu.addMenu(mode_menu)
        self.menu.addMenu(trans_menu)
        self.menu.addAction(self.act_lock)
        self.menu.addAction(self.act_click)
        self.menu.addSeparator()
        self.menu.addAction(self.act_sound)
        self.menu.addAction(self.vol_action)
        self.menu.addAction(self.size_action)
        self.menu.addAction(self.opacity_action)
        self.menu.addAction(self.act_startup)
        self.menu.addSeparator()
        self.menu.addAction(act_exit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def contextMenuEvent(self, event):
        self.menu.popup(event.globalPos())

    def _on_ram_updated(self, val, top_name="System", top_pct=0.0):
        self.raw_ram = val
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        if self.sound_enabled and val >= 90.0:
            self._trigger_ram_sound()

    def _trigger_ram_sound(self):
        now = time.time()
        if now - self.last_beep_time > self.beep_cooldown:
            self.last_beep_time = now
            self.sound_alert.play()

    def _update_ui_state(self):
        if not self.is_monitoring:
            return

        if self.transition_mode == "smooth":
            diff = self.raw_ram - self.displayed_ram
            self.displayed_ram += diff * 0.15
        else:
            self.displayed_ram = self.raw_ram

        c = self._get_color_for_percentage(self.displayed_ram, self.transition_mode)
        
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(c))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12)
        p.end()
        self.tray.setIcon(QIcon(pix))
        tip_text = f"RAM Usage: {self.displayed_ram:.1f}%\nTop RAM: {getattr(self, 'top_proc_name', 'System')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)"
        self.tray.setToolTip(tip_text)
        self.setToolTip(tip_text)

        self.setWindowOpacity(0.5 * (self.base_opacity / 100.0))
        self.update()

    def _get_color_for_percentage(self, pct, mode):
        if mode == "snappy":
            if pct < 45.0:
                return QColor(0, 230, 100)
            elif pct < 80.0:
                return QColor(255, 170, 0)
            else:
                return QColor(255, 45, 45)
        else:
            t = pct / 100.0
            if t < 0.5:
                sub_t = t * 2.0
                r = 0 + (240 - 0) * sub_t
                g = 230 + (180 - 230) * sub_t
                b = 100 + (0 - 100) * sub_t
            else:
                sub_t = (t - 0.5) * 2.0
                r = 240 + (255 - 240) * sub_t
                g = 180 + (45 - 180) * sub_t
                b = 0 + (45 - 0) * sub_t
            return QColor(int(r), int(g), int(b))

    def draw_animated_icon(self, p, cx, cy, pct, core_color):
        p.save()
        # 1. Draw the grey base icon as outline
        self.draw_icon_graphics(p, cx, cy, pct, QColor(130, 135, 145), fill=False)
        # 2. Draw the active color-filled overlay with bottom-up clipping as total fill
        fill_h = (pct / 100.0) * 16.0
        clip_rect = QRectF(cx - 10.0, cy + 8.0 - fill_h, 20.0, fill_h)
        p.setClipRect(clip_rect)
        self.draw_icon_graphics(p, cx, cy, pct, core_color, fill=True)
        p.restore()

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # RAM DIMM stick icon
        if fill:
            p.setBrush(QBrush(color))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 1.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

        # Main RAM PCB plate (horizontal)
        p.drawRect(QRectF(cx - 8.0, cy - 2.5, 16.0, 5.0))

        # Draw contacts (pins) at the bottom
        p.setPen(QPen(color, 1.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for px in [-6.0, -3.0, 0.0, 3.0, 6.0]:
            p.drawLine(QPointF(cx + px, cy + 2.5), QPointF(cx + px, cy + 3.8))

        # 4 blocks representing RAM chips
        lit_count = int((pct / 100.0) * 4.0 + 0.5)
        sweep = 0.5 + 0.5 * math.sin(time.time() * 3.5)

        chip_x_offsets = [-6.2, -2.5, 1.2, 4.9]
        for idx, offset in enumerate(chip_x_offsets):
            p.setPen(Qt.NoPen)
            if idx < lit_count:
                chip_alpha = int(180 + 75 * sweep)
                chip_c = QColor(255, 255, 255, chip_alpha) if fill else QColor(color.red(), color.green(), color.blue(), chip_alpha)
                p.setBrush(QBrush(chip_c))
            else:
                chip_c = QColor(255, 255, 255, 45) if fill else QColor(color.red(), color.green(), color.blue(), 45)
                p.setBrush(QBrush(chip_c))
            p.drawRect(QRectF(cx + offset, cy - 1.5, 1.5, 3.0))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Scale painter to support dynamic widget resizing
        scale_factor = self.widget_size / 60.0
        p.scale(scale_factor, scale_factor)

        cx = 30.0
        cy = 30.0
        r_base = 12.0

        core_color = self._get_color_for_percentage(self.displayed_ram, self.transition_mode)

        if self.display_mode == 2:
            glow_r = r_base + 8.0
            glow = QRadialGradient(cx, cy, glow_r)
            glow.setColorAt(0.0, QColor(core_color.red(), core_color.green(), core_color.blue(), 150))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        p.setPen(Qt.NoPen)
        if self.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, self.displayed_ram, core_color)
            return

        p.setBrush(QBrush(core_color))
        p.drawEllipse(QPointF(cx, cy), r_base, r_base)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        p.drawEllipse(QPointF(cx, cy), r_base - 0.5, r_base - 0.5)

        # 3. Draw Programmatic Animated RAM DIMM Stick in center
        p.setBrush(Qt.NoBrush)
        icon_color = QColor(255, 255, 255, 210)
        p.setPen(QPen(icon_color, 1.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

        # Main RAM PCB plate (horizontal)
        p.drawRect(QRectF(cx - 8.0, cy - 2.5, 16.0, 5.0))

        # Draw contacts (pins) at the bottom
        for px in [-6.0, -3.0, 0.0, 3.0, 6.0]:
            p.drawLine(QPointF(cx + px, cy + 2.5), QPointF(cx + px, cy + 3.8))

        # 4 blocks representing RAM chips, which light up based on usage + sweep animation
        lit_count = int((self.displayed_ram / 100.0) * 4.0 + 0.5)
        sweep = 0.5 + 0.5 * math.sin(time.time() * 3.5)

        chip_x_offsets = [-6.2, -2.5, 1.2, 4.9]
        for idx, offset in enumerate(chip_x_offsets):
            p.setPen(Qt.NoPen)
            # Decide color / brightness of each chip
            if idx < lit_count:
                # Active chip: bright white-ish, pulsing
                chip_alpha = int(180 + 75 * sweep)
                p.setBrush(QBrush(QColor(255, 255, 255, chip_alpha)))
            else:
                # Inactive chip: dark / semi-transparent
                p.setBrush(QBrush(QColor(255, 255, 255, 45)))
            p.drawRect(QRectF(cx + offset, cy - 1.5, 1.5, 3.0))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.is_locked:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and not self.is_locked:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.is_locked:
            config = load_config()
            config["x"] = self.x()
            config["y"] = self.y()
            save_config(config)
            event.accept()

    def start_monitoring(self):
        if not self.is_monitoring:
            self.is_monitoring = True
            self.monitor_worker.start()
            self.act_start.setEnabled(False)
            self.act_stop.setEnabled(True)

    def stop_monitoring(self):
        if self.is_monitoring:
            self.is_monitoring = False
            self.monitor_worker.stop()
            self.monitor_worker.wait()
            self.displayed_ram = 0.0
            self.act_start.setEnabled(True)
            self.act_stop.setEnabled(False)
            self.update()

    def change_mode(self, mode):
        self.display_mode = mode
        self.act_mode1.setChecked(mode == 1)
        self.act_mode2.setChecked(mode == 2)
        self.act_mode3.setChecked(mode == 3)
        self.update()
        config = load_config()
        config["display_mode"] = mode
        save_config(config)

    def change_transition(self, mode):
        self.transition_mode = mode
        self.act_trans_smooth.setChecked(mode == "smooth")
        self.act_trans_snappy.setChecked(mode == "snappy")
        config = load_config()
        config["transition_mode"] = mode
        save_config(config)

    def toggle_lock(self, checked):
        self.is_locked = checked
        self.act_lock.setChecked(checked)
        config = load_config()
        config["is_locked"] = checked
        save_config(config)

    def toggle_click_through(self, checked):
        self.click_through = checked
        self.act_click.setChecked(checked)
        config = load_config()
        config["click_through"] = checked
        save_config(config)

        self.hide()
        flags = (
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.SubWindow
        )
        if checked:
            flags |= Qt.WindowTransparentForInput
        
        self.setWindowFlags(flags)
        self.show()

    def toggle_sound(self, checked):
        self.sound_enabled = checked
        self.act_sound.setChecked(checked)
        config = load_config()
        config["sound_enabled"] = checked
        save_config(config)

    def change_volume(self, val):
        self.volume = val
        self.sound_alert.setVolume(val / 100.0)
        config = load_config()
        config["volume"] = val
        save_config(config)

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
        set_start_with_windows(checked)
        self.act_startup.setChecked(checked)

    def _restore_position(self, config):
        screen = QApplication.primaryScreen().geometry()
        default_x = screen.width() - self.width() - 50
        default_y = screen.height() - self.height() - 240
        x = config.get("x", default_x)
        y = config.get("y", default_y)
        self.move(x, y)

    def exit_app(self):
        self.monitor_worker.stop()
        self.monitor_worker.wait()
        QApplication.quit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app.setQuitOnLastWindowClosed(False)

    w = RAMUsageWidget()
    w.show()
    sys.exit(app.exec_())
