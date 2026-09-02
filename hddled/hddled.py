import sys
import os
import time
import math
import threading
import platform
import json
import struct
import io
import tempfile
import psutil
from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QAction, QActionGroup, QWidgetAction, QSlider, QHBoxLayout, QLabel
from PyQt5.QtCore import Qt, QPoint, QPointF, QTimer, pyqtSignal, QThread, QUrl
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient, QIcon, QPixmap
from PyQt5.QtMultimedia import QSoundEffect

# Try importing winreg on Windows for startup registry settings
_IS_WINDOWS = platform.system() == "Windows"
if _IS_WINDOWS:
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

APP_NAME = "HDDLEDMonitor"

# Registry helper functions for "Start with Windows"
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

# Persistent configuration helpers
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

# Generator for temporary WAV files (avoids winsound blocking beeps)
def generate_sine_wave_wav(frequency, duration_ms, sample_rate=22050):
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    buf = io.BytesIO()
    import wave
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1) # Mono
        wav.setsampwidth(2) # 16-bit
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


class DiskMonitorWorker(QThread):
    activity_detected = pyqtSignal(bool, bool)

    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True

    def run(self):
        try:
            prev_counters = psutil.disk_io_counters()
        except Exception:
            prev_counters = None

        while self._running:
            self.msleep(self.interval_ms)
            try:
                curr_counters = psutil.disk_io_counters()
            except Exception:
                continue

            if prev_counters is None or curr_counters is None:
                prev_counters = curr_counters
                continue

            has_read = curr_counters.read_bytes > prev_counters.read_bytes
            has_write = curr_counters.write_bytes > prev_counters.write_bytes

            if has_read or has_write:
                self.activity_detected.emit(has_read, has_write)

            prev_counters = curr_counters

    def stop(self):
        self._running = False


# QWidgetAction custom slider for dropdown menu
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
                background: #00ffb4;
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
                background: #00ffb4;
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
                background: #00ffb4;
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
                background: #00ffb4;
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
                background: #00ffb4;
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
                background: #00ffb4;
            }
        """)
        self.slider.valueChanged.connect(self._on_value_changed)
        lay.addWidget(self.slider)
        
        self.setDefaultWidget(self.widget)
        
    def _on_value_changed(self, val):
        if self.callback:
            self.callback(val)


class HDDLEDWidget(QWidget):
    def __init__(self):
        super().__init__()
        # Load saved settings
        config = load_config()
        self.is_locked = config.get("is_locked", False)
        self.click_through = config.get("click_through", False)
        self.display_mode = config.get("display_mode", 1) 
        self.sound_enabled = config.get("sound_enabled", True)
        self.volume = config.get("volume", 50) # Default volume 50%
        self.widget_size = config.get("widget_size", 60)
        self.base_opacity = config.get("base_opacity", 50)
        self.is_monitoring = True

        # Automatically register "Start with Windows" on startup
        if not config.get("startup_registered", False):
            set_start_with_windows(True)
            config["startup_registered"] = True
            save_config(config)

        # Generate audio assets in temp folder
        self.read_wav_path = create_temp_wav("read", 600, 25)
        self.write_wav_path = create_temp_wav("write", 1100, 25)

        # Initialize QSoundEffect instances
        self.sound_read = QSoundEffect(self)
        self.sound_read.setSource(QUrl.fromLocalFile(self.read_wav_path))
        self.sound_read.setVolume(self.volume / 100.0)

        self.sound_write = QSoundEffect(self)
        self.sound_write.setSource(QUrl.fromLocalFile(self.write_wav_path))
        self.sound_write.setVolume(self.volume / 100.0)

        # LED State variables
        self.is_reading = False
        self.is_writing = False
        self.last_activity_time = 0.0
        self.decay_ms = 150
        self.last_beep_time = 0.0
        self.beep_cooldown = 0.12 

        # Opacity variables
        self.current_opacity = 0.5
        self.target_opacity = 0.5

        # Initialize window
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        
        self.setFixedSize(self.widget_size, self.widget_size)
        self.setWindowTitle("HDD LED Monitor")

        # Position restore/setup
        self._restore_position(config)

        # UI Animation timer
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._update_ui_state)
        self.ui_timer.start(16)

        # Start Disk Monitor Thread
        self.monitor_worker = DiskMonitorWorker(interval_ms=50)
        self.monitor_worker.activity_detected.connect(self._on_disk_activity)
        if self.is_monitoring:
            self.monitor_worker.start()

        # System Tray
        self._setup_tray()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(120, 120, 120)))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12)
        p.end()
        
        self.tray.setIcon(QIcon(pix))
        self.tray.setToolTip("HDD LED Monitor")

        # Create context menu
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

        # Mode Selection
        mode_menu = QMenu("Display Mode", self)
        mode_group = QActionGroup(self)
        
        self.act_mode1 = QAction("Mode 1: Flat Dot", self, checkable=True)
        self.act_mode1.setChecked(self.display_mode == 1)
        self.act_mode1.triggered.connect(lambda: self.change_mode(1))
        
        self.act_mode2 = QAction("Mode 2: Activity Arrows", self, checkable=True)
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

        # Sound toggle
        self.act_sound = QAction("Sound On", self, checkable=True)
        self.act_sound.setChecked(self.sound_enabled)
        self.act_sound.triggered.connect(self.toggle_sound)

        # Volume sliders
        self.vol_action = VolumeSliderAction(self, self.volume, self.change_volume)
        self.size_action = SizeSliderAction(self, self.widget_size, self.change_widget_size)
        self.opacity_action = OpacitySliderAction(self, self.base_opacity, self.change_base_opacity)
 
        # Startup toggle
        self.act_startup = QAction("Start with Windows", self, checkable=True)
        self.act_startup.setChecked(is_start_with_windows_enabled())
        self.act_startup.triggered.connect(self.toggle_startup)
        
        act_exit = QAction("Exit", self)
        act_exit.triggered.connect(self.exit_app)
 
        self.menu.addAction(self.act_start)
        self.menu.addAction(self.act_stop)
        self.menu.addSeparator()
        self.menu.addMenu(mode_menu)
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

    def _on_disk_activity(self, has_read, has_write):
        if not self.is_monitoring:
            return
        self.last_activity_time = time.time()
        self.is_reading = has_read
        self.is_writing = has_write

        # Play WAV sound in background thread via QtMultimedia (non-blocking)
        if self.sound_enabled:
            self._play_activity_sound(has_read, has_write)

    def _play_activity_sound(self, has_read, has_write):
        now = time.time()
        if now - self.last_beep_time < self.beep_cooldown:
            return
        self.last_beep_time = now

        if has_read:
            self.sound_read.play()
        else:
            self.sound_write.play()

    def _update_ui_state(self):
        now = time.time()
        elapsed_ms = (now - self.last_activity_time) * 1000.0

        if elapsed_ms > self.decay_ms:
            self.is_reading = False
            self.is_writing = False

        # Apply target window opacity (50% default, 20% active)
        has_active_glow = self.is_reading or self.is_writing
        if has_active_glow:
            self.target_opacity = 0.2
        else:
            self.target_opacity = 0.5

        # Smoothly interpolate window opacity
        diff = self.target_opacity - self.current_opacity
        if abs(diff) > 0.01:
            self.current_opacity += diff * 0.15
        else:
            self.current_opacity = self.target_opacity

        opacity_mult = self.base_opacity / 100.0
        self.setWindowOpacity(self.current_opacity * opacity_mult)
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
        # Disk cylinder stack
        if fill:
            p.setBrush(QBrush(color))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx - 7, cy - 6, 14, 4))
            p.drawEllipse(QRectF(cx - 7, cy - 1, 14, 4))
            p.drawEllipse(QRectF(cx - 7, cy + 4, 14, 4))
            p.drawRect(QRectF(cx - 7, cy - 4, 14, 10))
        else:
            p.setPen(QPen(color, 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(cx - 7, cy - 6, 14, 4))
            p.drawEllipse(QRectF(cx - 7, cy - 1, 14, 4))
            p.drawEllipse(QRectF(cx - 7, cy + 4, 14, 4))
            p.drawLine(QPointF(cx - 7, cy - 4), QPointF(cx - 7, cy + 6))
            p.drawLine(QPointF(cx + 7, cy - 4), QPointF(cx + 7, cy + 6))
        
        if self.is_reading or self.is_writing:
            flash_color = QColor(200, 205, 215) if int(time.time() * 12) % 2 == 0 else QColor(90, 95, 100)
            p.setBrush(QBrush(flash_color))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx + 4, cy + 5), 1.5, 1.5)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Scale painter to support dynamic widget resizing
        scale_factor = self.widget_size / 60.0
        p.scale(scale_factor, scale_factor)

        cx, cy = 30.0, 30.0
        r_base = 10.0

        # 1. Draw Reduced Glow Effect (Tight Glow = r_base + 6)
        if self.is_reading or self.is_writing:
            glow_r = r_base + 6.0
            glow = QRadialGradient(cx, cy, glow_r)
            if self.is_reading:
                glow_color = QColor(255, 50, 50, 160) 
            else:
                glow_color = QColor(50, 255, 90, 160) 

            glow.setColorAt(0.0, glow_color)
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # 2. Draw Sphere or Circle Core
        p.setPen(Qt.NoPen)

        # Determine core base color
        if self.is_reading:
            core_color = QColor(255, 45, 45) 
        elif self.is_writing:
            core_color = QColor(35, 230, 75) 
        else:
            core_color = QColor(100, 100, 100) 

        pct = 100.0 if (self.is_reading or self.is_writing) else 0.0
        if self.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
            return

        # Mode 1 & 2: Flat Circle Core
        p.setBrush(QBrush(core_color))
        p.drawEllipse(QPointF(cx, cy), r_base, r_base)

        # Draw subtle inner ring border for flat circle
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        p.drawEllipse(QPointF(cx, cy), r_base - 0.5, r_base - 0.5)

        # 3. Mode 2: Draw Directional Activity Arrows
        if self.display_mode == 2:
            arrow_color = QColor(255, 255, 255, 240) if (self.is_reading or self.is_writing) else QColor(160, 160, 160, 140)
            pen = QPen(arrow_color, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)

            if self.is_reading:
                # Down Arrow (Read)
                p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx, cy + 4.5))
                p.drawLine(QPointF(cx, cy + 4.5), QPointF(cx - 2.5, cy + 2.0))
                p.drawLine(QPointF(cx, cy + 4.5), QPointF(cx + 2.5, cy + 2.0))
            elif self.is_writing:
                # Up Arrow (Write)
                p.drawLine(QPointF(cx, cy + 4.5), QPointF(cx, cy - 4.5))
                p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx - 2.5, cy - 2.0))
                p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx + 2.5, cy - 2.0))
            else:
                # Idle: Draw small side-by-side up and down arrows
                # Left Up Arrow
                p.drawLine(QPointF(cx - 2.5, cy + 3.5), QPointF(cx - 2.5, cy - 3.5))
                p.drawLine(QPointF(cx - 2.5, cy - 3.5), QPointF(cx - 4.0, cy - 1.5))
                p.drawLine(QPointF(cx - 2.5, cy - 3.5), QPointF(cx - 1.0, cy - 1.5))
                # Right Down Arrow
                p.drawLine(QPointF(cx + 2.5, cy - 3.5), QPointF(cx + 2.5, cy + 3.5))
                p.drawLine(QPointF(cx + 2.5, cy + 3.5), QPointF(cx + 1.0, cy + 1.5))
                p.drawLine(QPointF(cx + 2.5, cy + 3.5), QPointF(cx + 4.0, cy + 1.5))

    # --- Mouse Events for Dragging ---
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
            # Save final position to config
            config = load_config()
            config["x"] = self.x()
            config["y"] = self.y()
            save_config(config)
            event.accept()

    # --- Actions Handlers ---
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
            self.is_reading = False
            self.is_writing = False
            self.act_start.setEnabled(True)
            self.act_stop.setEnabled(False)
            self.update()

    def change_mode(self, mode):
        self.display_mode = mode
        self.act_mode1.setChecked(mode == 1)
        self.act_mode2.setChecked(mode == 2)
        self.act_mode3.setChecked(mode == 3)
        self.update()
        
        # Save to config
        config = load_config()
        config["display_mode"] = mode
        save_config(config)

    def toggle_lock(self, checked):
        self.is_locked = checked
        self.act_lock.setChecked(checked)
        
        # Save to config
        config = load_config()
        config["is_locked"] = checked
        save_config(config)

    def toggle_click_through(self, checked):
        self.click_through = checked
        self.act_click.setChecked(checked)
        
        # Save to config
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

        # Save to config
        config = load_config()
        config["sound_enabled"] = checked
        save_config(config)

    def change_volume(self, val):
        self.volume = val
        self.sound_read.setVolume(val / 100.0)
        self.sound_write.setVolume(val / 100.0)
 
        # Save to config
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
        default_y = screen.height() - self.height() - 100
        
        # Load position from config, fallback to default
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

    w = HDDLEDWidget()
    w.show()
    sys.exit(app.exec_())
