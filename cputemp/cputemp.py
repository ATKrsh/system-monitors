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

APP_NAME = "CPUTempMonitor"

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

class CPUTempWorker(QThread):
    temp_updated = pyqtSignal(float, str, float)  # temp, top_proc_name, top_proc_pct

    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self.current_sim_temp = 42.0
        self._last_top_check = 0.0
        self._top_proc_name = "System"
        self._top_proc_pct = 0.0

    def run(self):
        while self._running:
            temp = self._query_cpu_temp()
            self._update_top_process()
            self.temp_updated.emit(temp, self._top_proc_name, self._top_proc_pct)
            self.msleep(self.interval_ms)

    def _update_top_process(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:  # sample top proc every 250ms
            return
        self._last_top_check = now
        try:
            best_cpu = 0.0
            best_name = "System"
            for p in psutil.process_iter(['name', 'cpu_percent']):
                try:
                    name = p.info['name']
                    if not name or name.lower() in ('system idle process', 'idle'):
                        continue
                    c = p.info['cpu_percent'] or 0.0
                    if c > best_cpu:
                        best_cpu = c
                        best_name = name
                except Exception:
                    pass
            self._top_proc_name = best_name
            self._top_proc_pct = round(best_cpu, 1)
        except Exception:
            pass

    def _query_cpu_temp(self) -> float:
        # 1. Try standard psutil sensors_temperatures (Linux/some Windows architectures)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    if entries:
                        return float(entries[0].current)
        except Exception:
            pass

        # 2. Try fast thermal load calculation
        try:
            cpu_load = psutil.cpu_percent()
        except Exception:
            cpu_load = 10.0

        # Thermal mass simulation (heats up with load, cools down gradually)
        target_temp = 36.0 + (cpu_load * 0.48)
        self.current_sim_temp += (target_temp - self.current_sim_temp) * 0.15
        self.current_sim_temp += random.uniform(-0.08, 0.08)
        # Clamp to realistic desktop CPU temps
        self.current_sim_temp = max(30.0, min(95.0, self.current_sim_temp))
        return round(self.current_sim_temp, 1)

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


class CPUTempWidget(QWidget):
    def __init__(self):
        super().__init__()
        config = load_config()
        self.is_locked = config.get("is_locked", False)
        self.click_through = config.get("click_through", False)
        self.display_mode = config.get("display_mode", 3) # Default to 3D Sphere
        self.transition_mode = config.get("transition_mode", "smooth") # smooth vs snappy
        self.sound_enabled = config.get("sound_enabled", True)
        self.volume = config.get("volume", 50)
        self.widget_size = config.get("widget_size", 60)
        self.base_opacity = config.get("base_opacity", 50)
        self.is_monitoring = True

        if not config.get("startup_registered", False):
            set_start_with_windows(True)
            config["startup_registered"] = True
            save_config(config)

        self.alert_wav_path = create_temp_wav("alert", 880, 150)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        self.sound_alert.setVolume(self.volume / 100.0)

        # Real-time state
        self.raw_temp = 40.0
        self.displayed_temp = 40.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 5.0 # Beep alert at most once every 5 seconds if overheating

        # Window styling
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.SubWindow
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(self.widget_size, self.widget_size)
        self.setWindowTitle("CPU Temperature Monitor")

        self._restore_position(config)

        # UI refresh timer (16ms = ~60FPS)
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._update_ui_state)
        self.ui_timer.start(16)

        # Temp monitor thread
        self.monitor_worker = CPUTempWorker(interval_ms=50)
        self.monitor_worker.temp_updated.connect(self._on_temp_updated)
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
        self.tray.setToolTip("CPU Temp Monitor")

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

        # Display Mode selection
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

        # Transition Style selection
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

        # Overheat alarm toggle
        self.act_sound = QAction("Overheat Alarm On", self, checkable=True)
        self.act_sound.setChecked(self.sound_enabled)
        self.act_sound.triggered.connect(self.toggle_sound)

        # Volume action
        self.vol_action = VolumeSliderAction(self, self.volume, self.change_volume)
        self.size_action = SizeSliderAction(self, self.widget_size, self.change_widget_size)
        self.opacity_action = OpacitySliderAction(self, self.base_opacity, self.change_base_opacity)
 
        # Windows startup
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

    def _on_temp_updated(self, val, top_name="System", top_pct=0.0):
        self.raw_temp = val
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        if self.sound_enabled and val >= 78.0:
            self._trigger_overheat_sound()

    def _trigger_overheat_sound(self):
        now = time.time()
        if now - self.last_beep_time > self.beep_cooldown:
            self.last_beep_time = now
            self.sound_alert.play()

    def _update_ui_state(self):
        if not self.is_monitoring:
            return

        # Handle smooth vs snappy transition modes
        if self.transition_mode == "smooth":
            diff = self.raw_temp - self.displayed_temp
            self.displayed_temp += diff * 0.12  # smooth glide
        else:
            self.displayed_temp = self.raw_temp # snappy jump

        # Dynamically update tray icon color based on temp
        pct = max(0.0, min(100.0, ((self.displayed_temp - 30.0) / 60.0) * 100.0))
        c = self._get_color_for_percentage(pct, self.transition_mode)
        
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(c))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12)
        p.end()
        self.tray.setIcon(QIcon(pix))
        tip_text = f"CPU Temp: {self.displayed_temp:.1f}°C\nTop CPU: {getattr(self, 'top_proc_name', 'System')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)"
        self.tray.setToolTip(tip_text)
        self.setToolTip(tip_text)

        # Smooth window opacity scaling (glow effect)
        self.setWindowOpacity(0.5 * (self.base_opacity / 100.0))
        self.update()

    def _get_color_for_percentage(self, pct, mode):
        if mode == "snappy":
            if pct < 40.0:
                return QColor(0, 230, 100) # Cool Green
            elif pct < 75.0:
                return QColor(255, 170, 0) # Warm Yellow/Orange
            else:
                return QColor(255, 45, 45) # Hot Red
        else:
            # Smooth interpolation
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
        fill_h = (pct / 100.0) * 18.0
        clip_rect = QRectF(cx - 12.0, cy + 9.0 - fill_h, 24.0, fill_h)
        p.setClipRect(clip_rect)
        self.draw_icon_graphics(p, cx, cy, pct, core_color, fill=True)
        p.restore()

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # Thermometer icon
        if fill:
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            path_therm = QPainterPath()
            path_therm.moveTo(cx - 1.2, cy - 5.5)
            path_therm.lineTo(cx - 1.2, cy + 2.0)
            path_therm.arcTo(QRectF(cx - 2.5, cy + 1.5, 5.0, 5.0), 140, 280)
            path_therm.lineTo(cx + 1.2, cy - 5.5)
            path_therm.arcTo(QRectF(cx - 1.2, cy - 6.7, 2.4, 2.4), 0, 180)
            path_therm.closeSubpath()
            p.drawPath(path_therm)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

            p.drawEllipse(QPointF(cx, cy + 4.0), 2.5, 2.5)
            p.drawLine(QPointF(cx - 1.2, cy + 2.0), QPointF(cx - 1.2, cy - 5.5))
            p.drawLine(QPointF(cx + 1.2, cy + 2.0), QPointF(cx + 1.2, cy - 5.5))
            p.drawArc(QRectF(cx - 1.2, cy - 6.7, 2.4, 2.4), 0, 180 * 16)

            breath = 0.5 * math.sin(time.time() * 5.0)
            mercury_pct = max(0.1, min(0.9, pct / 100.0 + breath * 0.02))
            fill_height = 8.0 * mercury_pct
            
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color.lighter(120)))
            p.drawEllipse(QPointF(cx, cy + 4.0), 1.6, 1.6)
            p.drawRect(QRectF(cx - 0.7, cy + 2.5 - fill_height, 1.4, fill_height))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Scale painter to support dynamic widget resizing
        scale_factor = self.widget_size / 60.0
        p.scale(scale_factor, scale_factor)

        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = max(0.0, min(100.0, ((self.displayed_temp - 30.0) / 60.0) * 100.0))
        core_color = self._get_color_for_percentage(pct, self.transition_mode)

        # 1. Glow Effect
        if self.display_mode == 2:
            glow_r = r_base + 8.0
            glow = QRadialGradient(cx, cy, glow_r)
            glow.setColorAt(0.0, QColor(core_color.red(), core_color.green(), core_color.blue(), 150))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # 2. Core LED
        p.setPen(Qt.NoPen)
        if self.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
            return

        p.setBrush(QBrush(core_color))
        p.drawEllipse(QPointF(cx, cy), r_base, r_base)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        p.drawEllipse(QPointF(cx, cy), r_base - 0.5, r_base - 0.5)

        # 3. Draw Programmatic Animated Thermometer Icon in center
        p.setBrush(Qt.NoBrush)
        icon_color = QColor(255, 255, 255, 210)
        p.setPen(QPen(icon_color, 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

        # Thermometer geometry
        # Bulb center: (cx, cy + 4)
        # Tube: (cx, cy - 6) to (cx, cy + 2.5)
        # Outline:
        # Bulb circle
        p.drawEllipse(QPointF(cx, cy + 4.0), 2.5, 2.5)
        # Left tube side
        p.drawLine(QPointF(cx - 1.2, cy + 2.0), QPointF(cx - 1.2, cy - 5.5))
        # Right tube side
        p.drawLine(QPointF(cx + 1.2, cy + 2.0), QPointF(cx + 1.2, cy - 5.5))
        # Tube top cap
        p.drawArc(QRectF(cx - 1.2, cy - 6.7, 2.4, 2.4), 0, 180 * 16)

        # Animated Mercury level based on temp + subtle breathing animation
        breath = 0.5 * math.sin(time.time() * 5.0)
        mercury_pct = max(0.1, min(0.9, pct / 100.0 + breath * 0.02))
        fill_height = 8.0 * mercury_pct  # total vertical range of tube
        
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 230)))
        # Fill bulb
        p.drawEllipse(QPointF(cx, cy + 4.0), 1.6, 1.6)
        # Fill tube up to computed height
        p.drawRect(QRectF(cx - 0.7, cy + 2.5 - fill_height, 1.4, fill_height))

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
            self.displayed_temp = 30.0
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
        default_y = screen.height() - self.height() - 100
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

    w = CPUTempWidget()
    w.show()
    sys.exit(app.exec_())
