import sys
import os
import time
import math
import random
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

# Try importing winreg on Windows for startup registry settings
_IS_WINDOWS = platform.system() == "Windows"
if _IS_WINDOWS:
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

APP_NAME = "UnifiedSystemMonitors"

# Estimated TDP scaling by detected CPU core count
def _estimate_cpu_tdp():
    count = psutil.cpu_count(logical=False) or 4
    if count >= 16: return 150.0
    if count >= 8:  return 95.0
    if count >= 6:  return 65.0
    return 45.0

CPU_TDP = _estimate_cpu_tdp()
POWER_MIN = 5.0
POWER_MAX = CPU_TDP + 130.0 + 30.0

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
            try: winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError: pass
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


# ─────────────────────────────────────────────────────────────────────────────
# Worker Threads
# ─────────────────────────────────────────────────────────────────────────────

class DiskMonitorWorker(QThread):
    activity_detected = pyqtSignal(bool, bool)
    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
    def run(self):
        try: prev_counters = psutil.disk_io_counters()
        except: prev_counters = None
        while self._running:
            self.msleep(self.interval_ms)
            try: curr_counters = psutil.disk_io_counters()
            except: continue
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

class NetMonitorWorker(QThread):
    activity_detected = pyqtSignal(bool, bool)
    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
    def run(self):
        try: prev_counters = psutil.net_io_counters()
        except: prev_counters = None
        while self._running:
            self.msleep(self.interval_ms)
            try: curr_counters = psutil.net_io_counters()
            except: continue
            if prev_counters is None or curr_counters is None:
                prev_counters = curr_counters
                continue
            has_download = curr_counters.bytes_recv > prev_counters.bytes_recv
            has_upload = curr_counters.bytes_sent > prev_counters.bytes_sent
            if has_download or has_upload:
                self.activity_detected.emit(has_download, has_upload)
            prev_counters = curr_counters
    def stop(self):
        self._running = False

class CPUTempWorker(QThread):
    temp_updated = pyqtSignal(float, str, float)  # temp, top_name, top_pct
    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self.current_sim_temp = 42.0
        self._last_top_check = 0.0
        self._top_name = "System"
        self._top_pct = 0.0

    def run(self):
        while self._running:
            temp = self._query_cpu_temp()
            self._update_top_cpu()
            self.temp_updated.emit(temp, self._top_name, self._top_pct)
            self.msleep(self.interval_ms)

    def _update_top_cpu(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:
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
            self._top_name = best_name
            self._top_pct = round(best_cpu, 1)
        except Exception:
            pass

    def _query_cpu_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    if entries: return float(entries[0].current)
        except: pass
        try: cpu_load = psutil.cpu_percent()
        except: cpu_load = 10.0
        target_temp = 36.0 + (cpu_load * 0.48)
        self.current_sim_temp += (target_temp - self.current_sim_temp) * 0.15
        self.current_sim_temp += random.uniform(-0.08, 0.08)
        return round(max(30.0, min(95.0, self.current_sim_temp)), 1)
    def stop(self):
        self._running = False

class CPULoadWorker(QThread):
    load_updated = pyqtSignal(float, str, float)  # load, top_name, top_pct
    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self._last_top_check = 0.0
        self._top_name = "System"
        self._top_pct = 0.0

    def run(self):
        try: psutil.cpu_percent()
        except: pass
        while self._running:
            try: load = psutil.cpu_percent()
            except: load = 0.0
            self._update_top_cpu()
            self.load_updated.emit(load, self._top_name, self._top_pct)
            self.msleep(self.interval_ms)

    def _update_top_cpu(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:
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
            self._top_name = best_name
            self._top_pct = round(best_cpu, 1)
        except Exception:
            pass

    def stop(self):
        self._running = False

class RAMUsageWorker(QThread):
    ram_updated = pyqtSignal(float, str, float)  # ram_pct, top_name, top_pct
    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self._last_top_check = 0.0
        self._top_name = "System"
        self._top_pct = 0.0

    def run(self):
        while self._running:
            try: ram_pct = psutil.virtual_memory().percent
            except: ram_pct = 0.0
            self._update_top_ram()
            self.ram_updated.emit(ram_pct, self._top_name, self._top_pct)
            self.msleep(self.interval_ms)

    def _update_top_ram(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:
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
            self._top_name = best_name
            self._top_pct = round(best_mem, 1)
        except Exception:
            pass

    def stop(self):
        self._running = False

class PowerWorker(QThread):
    power_updated = pyqtSignal(float, str, float, str, float)  # watts, top_cpu, top_cpu_p, top_gpu, top_gpu_p
    def __init__(self, interval_ms=50):
        super().__init__()
        self.interval_ms = interval_ms
        self._running = True
        self._gpu_watts = 0.0
        self._has_nvml = False
        self._nvml_dev = None
        self._nvml = None
        try:
            import ctypes
            self._nvml = ctypes.CDLL("nvml.dll")
            if self._nvml.nvmlInit_v2() == 0:
                self._nvml_dev = ctypes.c_void_p()
                if self._nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._nvml_dev)) == 0:
                    self._has_nvml = True
        except: pass

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
        except: pass

        self._last_top_check = 0.0
        self._top_cpu_name = "System"
        self._top_cpu_pct = 0.0
        self._top_gpu_name = "Idle"
        self._top_gpu_pct = 0.0

    def run(self):
        try: psutil.cpu_percent()
        except: pass
        while self._running:
            watts = self._query_power()
            self._update_top_procs()
            self.power_updated.emit(watts, self._top_cpu_name, self._top_cpu_pct, self._top_gpu_name, self._top_gpu_pct)
            self.msleep(self.interval_ms)

    def _update_top_procs(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:
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
            self._top_cpu_name = best_name
            self._top_cpu_pct = round(best_cpu, 1)
        except Exception:
            pass

        if self._has_pdh and self._h_query:
            try:
                import ctypes
                from ctypes import wintypes
                self.pdh.PdhCollectQueryData(self._h_query)
                buf_size = wintypes.DWORD(0)
                item_count = wintypes.DWORD(0)
                self.pdh.PdhGetFormattedCounterArrayW(
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
                        gpu_by_pid = {}
                        for i in range(item_count.value):
                            v = items[i].FmtValue.doubleValue
                            name = items[i].szName
                            if v > 0.5 and "pid_" in name:
                                try:
                                    pid = int(name.split("pid_")[1].split("_")[0])
                                    if pid > 0 and pid != 4:
                                        gpu_by_pid[pid] = gpu_by_pid.get(pid, 0.0) + v
                                except Exception:
                                    pass
                        if gpu_by_pid:
                            top_pid, top_util = max(gpu_by_pid.items(), key=lambda x: x[1])
                            try:
                                pname = psutil.Process(top_pid).name()
                            except Exception:
                                pname = f"PID {top_pid}"
                            self._top_gpu_name = pname
                            self._top_gpu_pct = round(top_util, 1)
                        else:
                            self._top_gpu_name = "Idle"
                            self._top_gpu_pct = 0.0
            except Exception:
                pass

    def _query_power(self) -> float:
        try: cpu_pct = psutil.cpu_percent()
        except: cpu_pct = 10.0
        cpu_watts = 5.0 + (cpu_pct / 100.0) * (CPU_TDP - 5.0)
        if self._has_nvml and self._nvml_dev:
            try:
                import ctypes
                pwr_val = ctypes.c_uint()
                if self._nvml.nvmlDeviceGetPowerUsage(self._nvml_dev, ctypes.byref(pwr_val)) == 0:
                    self._gpu_watts = float(pwr_val.value) / 1000.0
            except: pass
        else:
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=0.5, creationflags=0x08000000
                )
                if result.returncode == 0:
                    self._gpu_watts = float(result.stdout.strip().split()[0])
            except: pass
        return round(cpu_watts + self._gpu_watts + 18.0, 1)
    def stop(self):
        self._running = False
        if self._has_pdh and self._h_query:
            try: self.pdh.PdhCloseQuery(self._h_query)
            except: pass
            self._h_query = None

class GPUTempWorker(QThread):
    temp_updated = pyqtSignal(float, float, str, str, str, float)  # dgpu_temp, igpu_temp, dgpu_name, igpu_name, top_gpu_name, top_gpu_pct
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
        except: pass

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
        except: pass

        self._last_dgpu_temp = 50.0
        self._last_igpu_temp = 42.0
        self._last_top_check = 0.0
        self._top_gpu_name = "Idle"
        self._top_gpu_pct = 0.0

    def run(self):
        while self._running:
            dgpu_t, igpu_t = self._query_gpu_temps()
            self._update_top_gpu()
            self.temp_updated.emit(dgpu_t, igpu_t, self.dgpu_name, self.igpu_name, self._top_gpu_name, self._top_gpu_pct)
            self.msleep(self.interval_ms)

    def _update_top_gpu(self):
        now = time.monotonic()
        if now - self._last_top_check < 0.25:
            return
        self._last_top_check = now
        if self._has_pdh and self._h_query:
            try:
                import ctypes
                from ctypes import wintypes
                self.pdh.PdhCollectQueryData(self._h_query)
                buf_size = wintypes.DWORD(0)
                item_count = wintypes.DWORD(0)
                self.pdh.PdhGetFormattedCounterArrayW(
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
                        gpu_by_pid = {}
                        for i in range(item_count.value):
                            v = items[i].FmtValue.doubleValue
                            name = items[i].szName
                            if v > 0.5 and "pid_" in name:
                                try:
                                    pid = int(name.split("pid_")[1].split("_")[0])
                                    if pid > 0 and pid != 4:
                                        gpu_by_pid[pid] = gpu_by_pid.get(pid, 0.0) + v
                                except Exception:
                                    pass
                        if gpu_by_pid:
                            top_pid, top_util = max(gpu_by_pid.items(), key=lambda x: x[1])
                            try:
                                pname = psutil.Process(top_pid).name()
                            except Exception:
                                pname = f"PID {top_pid}"
                            self._top_gpu_name = pname
                            self._top_gpu_pct = round(top_util, 1)
                        else:
                            self._top_gpu_name = "Idle"
                            self._top_gpu_pct = 0.0
            except Exception:
                pass

    def _query_gpu_temps(self) -> tuple:
        if self._has_nvml and self._nvml_dev:
            try:
                import ctypes
                temp_val = ctypes.c_uint()
                if self._nvml.nvmlDeviceGetTemperature(self._nvml_dev, 0, ctypes.byref(temp_val)) == 0:
                    self._last_dgpu_temp = float(temp_val.value)
            except: pass
        else:
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=0.5, creationflags=0x08000000
                )
                if result.returncode == 0:
                    val = result.stdout.strip()
                    if val: self._last_dgpu_temp = float(val)
            except: pass

        try: cpu_load = psutil.cpu_percent()
        except: cpu_load = 10.0
        target_igpu = 35.0 + (cpu_load * 0.38)
        self._last_igpu_temp += (target_igpu - self._last_igpu_temp) * 0.12
        self._last_igpu_temp = round(max(30.0, min(90.0, self._last_igpu_temp)), 1)
        return self._last_dgpu_temp, self._last_igpu_temp

    def stop(self):
        self._running = False
        if self._has_pdh and self._h_query:
            try: self.pdh.PdhCloseQuery(self._h_query)
            except: pass
            self._h_query = None

class GPUUsageWorker(QThread):
    usage_updated = pyqtSignal(float, float, float, str, str, str, float)  # dgpu_u, igpu_u, total_u, dgpu_name, igpu_name, top_gpu_name, top_gpu_pct
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
        except: pass

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
        except: pass

        self._last_dgpu_util = 0.0
        self._last_igpu_util = 0.0
        self._last_total_util = 0.0
        self._top_gpu_name = "Idle"
        self._top_gpu_pct = 0.0

    def run(self):
        while self._running:
            dgpu_u, igpu_u, total_u = self._query_gpu_util()
            self.usage_updated.emit(dgpu_u, igpu_u, total_u, self.dgpu_name, self.igpu_name, self._top_gpu_name, self._top_gpu_pct)
            self.msleep(self.interval_ms)

    def _query_gpu_util(self) -> tuple:
        if self._has_nvml and self._nvml_dev:
            try:
                import ctypes
                class nvmlUtil(ctypes.Structure):
                    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]
                util_val = nvmlUtil()
                if self._nvml.nvmlDeviceGetUtilizationRates(self._nvml_dev, ctypes.byref(util_val)) == 0:
                    self._last_dgpu_util = float(util_val.gpu)
            except: pass
        else:
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=0.5, creationflags=0x08000000
                )
                if result.returncode == 0:
                    val = result.stdout.strip()
                    if val: self._last_dgpu_util = float(val)
            except: pass

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
            except: pass

        if gpu_by_pid:
            top_pid, top_util = max(gpu_by_pid.items(), key=lambda x: x[1])
            try:
                self._top_gpu_name = psutil.Process(top_pid).name()
            except Exception:
                self._top_gpu_name = f"PID {top_pid}"
            self._top_gpu_pct = round(top_util, 1)
        elif self._last_dgpu_util > 0:
            self._top_gpu_pct = round(self._last_dgpu_util, 1)
        else:
            self._top_gpu_name = "Idle"
            self._top_gpu_pct = 0.0

        self._last_total_util = max(self._last_dgpu_util, min(100.0, total_pdh))
        self._last_igpu_util = max(0.0, min(100.0, total_pdh - self._last_dgpu_util))
        return self._last_dgpu_util, self._last_igpu_util, self._last_total_util

    def stop(self):
        self._running = False
        if self._has_pdh and self._h_query:
            try: self.pdh.PdhCloseQuery(self._h_query)
            except: pass
            self._h_query = None


# ─────────────────────────────────────────────────────────────────────────────
# Base Monitor Widget Class
# ─────────────────────────────────────────────────────────────────────────────

class BaseMonitorWidget(QWidget):
    def __init__(self, parent_app, title):
        super().__init__()
        self.parent_app = parent_app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        w_size = getattr(self.parent_app, "widget_size", 60)
        self.setFixedSize(w_size, w_size)
        self.setWindowTitle(title)
        
        self.is_monitoring = True
        self.current_opacity = 0.5
        self.target_opacity = 0.5
        
        # UI update timer
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._base_update_ui)
        self.ui_timer.start(16)

    def contextMenuEvent(self, event):
        self.parent_app.menu.popup(event.globalPos())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.parent_app.is_locked:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and not self.parent_app.is_locked:
            new_tl = event.globalPos() - self._drag_pos
            self.parent_app.handle_widget_dragged(self, new_tl)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.parent_app.is_locked:
            self.parent_app.save_current_layout()
            event.accept()

    def _base_update_ui(self):
        self.update_widget_state()
        
        # Smooth window opacity interpolation
        diff = self.target_opacity - self.current_opacity
        if abs(diff) > 0.01:
            self.current_opacity += diff * 0.15
        else:
            self.current_opacity = self.target_opacity
            
        opacity_mult = getattr(self.parent_app, "base_opacity", 50) / 100.0
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

    def update_widget_state(self):
        pass

    def get_current_percentage(self) -> float:
        return 0.0

    def get_color_for_percentage(self, pct):
        # Generic fallback – individual widgets override with their own palette
        if self.parent_app.transition_mode == "snappy":
            if pct < 40.0:   return QColor(0, 230, 100)
            elif pct < 75.0: return QColor(255, 170, 0)
            else:            return QColor(255, 45, 45)
        t = pct / 100.0
        if t < 0.5:
            s = t * 2.0
            return QColor(int(0+240*s), int(230+(180-230)*s), int(100-100*s))
        s = (t - 0.5) * 2.0
        return QColor(int(240+15*s), int(180+(45-180)*s), int(45*s))

    def lerp_color(self, c1, c2, t):
        """Linearly interpolate between two QColors."""
        t = max(0.0, min(1.0, t))
        return QColor(
            int(c1.red()   + (c2.red()   - c1.red())   * t),
            int(c1.green() + (c2.green() - c1.green()) * t),
            int(c1.blue()  + (c2.blue()  - c1.blue())  * t),
        )

    def draw_core_led(self, p, cx, cy, r_base, core_color, pct):
        p.setPen(Qt.NoPen)
        grey_bg = QColor(60, 64, 72)
        clip_path = QPainterPath()
        clip_path.addEllipse(QPointF(cx, cy), r_base, r_base)
        p.save()
        p.setClipPath(clip_path)
        if self.parent_app.display_mode == 3:
            grad_bg = QRadialGradient(cx, cy, r_base, cx - r_base/3.0, cy - r_base/3.0)
            grad_bg.setColorAt(0.0, QColor(160, 165, 175, 230))
            grad_bg.setColorAt(0.2, grey_bg.lighter(130))
            grad_bg.setColorAt(0.7, grey_bg)
            grad_bg.setColorAt(1.0, grey_bg.darker(160))
            p.setBrush(QBrush(grad_bg))
        else:
            p.setBrush(QBrush(grey_bg))
        p.drawRect(int(cx - r_base - 1), int(cy - r_base - 1), int(r_base * 2 + 2), int(r_base * 2 + 2))
        if pct > 0:
            fill_height = (pct / 100.0) * (r_base * 2)
            if self.parent_app.display_mode == 3:
                grad_active = QRadialGradient(cx, cy, r_base, cx - r_base/3.0, cy - r_base/3.0)
                grad_active.setColorAt(0.0, QColor(255, 255, 255, 230))
                grad_active.setColorAt(0.2, core_color.lighter(130))
                grad_active.setColorAt(0.7, core_color)
                grad_active.setColorAt(1.0, core_color.darker(160))
                p.setBrush(QBrush(grad_active))
            else:
                p.setBrush(QBrush(core_color))
            p.drawRect(int(cx - r_base - 1), int(cy + r_base - fill_height), int(r_base * 2 + 2), int(fill_height))
        p.restore()
        if self.parent_app.display_mode != 3:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
            p.drawEllipse(QPointF(cx, cy), r_base - 0.5, r_base - 0.5)

    def draw_glow(self, p, cx, cy, r_base, core_color):
        pass  # glow disabled globally

    def draw_center_text(self, p, cx, cy, text_str):
        """Draw bold white text with dark outline, perfectly centred in the LED."""
        font = p.font()
        font.setFamily("Segoe UI")
        font.setBold(True)
        n = len(text_str)
        if n >= 4:
            font.setPixelSize(6)
        elif n == 3:
            font.setPixelSize(7)
        elif n == 2:
            font.setPixelSize(9)
        else:
            font.setPixelSize(11)
        p.setFont(font)

        fm = p.fontMetrics()
        try:
            text_w = fm.horizontalAdvance(text_str)
        except AttributeError:
            text_w = fm.width(text_str)
        text_h = fm.ascent() - fm.descent()

        tx = cx - text_w / 2.0
        ty = cy + text_h / 2.0

        path = QPainterPath()
        path.addText(tx, ty, font, text_str)

        p.setRenderHint(QPainter.TextAntialiasing)
        p.setPen(QPen(QColor(0, 0, 0, 200), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.drawPath(path)


# ─────────────────────────────────────────────────────────────────────────────
# 1. HDD LED Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class HDDLEDWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "HDD LED Monitor")
        
        # Audio
        self.read_wav_path = create_temp_wav("read", 600, 25)
        self.write_wav_path = create_temp_wav("write", 1100, 25)
        self.sound_read = QSoundEffect(self)
        self.sound_read.setSource(QUrl.fromLocalFile(self.read_wav_path))
        self.sound_write = QSoundEffect(self)
        self.sound_write.setSource(QUrl.fromLocalFile(self.write_wav_path))
        
        self.is_reading = False
        self.is_writing = False
        self.last_activity_time = 0.0
        self.decay_ms = 150
        self.last_beep_time = 0.0
        self.beep_cooldown = 0.12
        
        self.monitor_worker = DiskMonitorWorker(interval_ms=50)
        self.monitor_worker.activity_detected.connect(self._on_disk_activity)
        self.monitor_worker.start()

    def _on_disk_activity(self, has_read, has_write):
        if not self.is_monitoring or not self.isVisible(): return
        self.last_activity_time = time.time()
        self.is_reading = has_read
        self.is_writing = has_write
        
        if self.parent_app.sound_enabled:
            now = time.time()
            if now - self.last_beep_time >= self.beep_cooldown:
                self.last_beep_time = now
                if has_read: self.sound_read.play()
                else: self.sound_write.play()

    def update_widget_state(self):
        elapsed_ms = (time.time() - self.last_activity_time) * 1000.0
        if elapsed_ms > self.decay_ms:
            self.is_reading = False
            self.is_writing = False
        self.target_opacity = 0.2 if (self.is_reading or self.is_writing) else 0.5

    def get_current_percentage(self) -> float:
        return 100.0 if (self.is_reading or self.is_writing) else 0.0

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
        # HDD LED — Scheme: Crimson (read) / Lime (write) / Slate (idle)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        if self.is_reading:    core_color = QColor(220, 50, 50)    # crimson
        elif self.is_writing:  core_color = QColor(50, 210, 100)   # lime
        else:                  core_color = QColor(80, 90, 100)     # slate

        pct = self.get_current_percentage()
        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Network Activity LED Widget
# ─────────────────────────────────────────────────────────────────────────────

class NETLEDWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "Net LED Monitor")
        
        self.down_wav_path = create_temp_wav("down", 700, 25)
        self.up_wav_path = create_temp_wav("up", 1200, 25)
        self.sound_down = QSoundEffect(self)
        self.sound_down.setSource(QUrl.fromLocalFile(self.down_wav_path))
        self.sound_up = QSoundEffect(self)
        self.sound_up.setSource(QUrl.fromLocalFile(self.up_wav_path))
        
        self.is_downloading = False
        self.is_uploading = False
        self.last_activity_time = 0.0
        self.decay_ms = 150
        self.last_beep_time = 0.0
        self.beep_cooldown = 0.12
        
        self.monitor_worker = NetMonitorWorker(interval_ms=50)
        self.monitor_worker.activity_detected.connect(self._on_net_activity)
        self.monitor_worker.start()

    def _on_net_activity(self, has_download, has_upload):
        if not self.is_monitoring or not self.isVisible(): return
        self.last_activity_time = time.time()
        self.is_downloading = has_download
        self.is_uploading = has_upload
        
        if self.parent_app.sound_enabled:
            now = time.time()
            if now - self.last_beep_time >= self.beep_cooldown:
                self.last_beep_time = now
                if has_download: self.sound_down.play()
                else: self.sound_up.play()

    def update_widget_state(self):
        elapsed_ms = (time.time() - self.last_activity_time) * 1000.0
        if elapsed_ms > self.decay_ms:
            self.is_downloading = False
            self.is_uploading = False
        self.target_opacity = 0.2 if (self.is_downloading or self.is_uploading) else 0.5

    def get_current_percentage(self) -> float:
        return 100.0 if (self.is_downloading or self.is_uploading) else 0.0

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # Two arrows moving UP/DOWN
        up_offset = 0.0
        down_offset = 0.0
        if self.is_uploading:
            up_offset = -3.0 * ((time.time() * 5.0) % 1.0)
        if self.is_downloading:
            down_offset = 3.0 * ((time.time() * 5.0) % 1.0)

        if fill:
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            
            # Left Up Arrow (Upload)
            path_up = QPainterPath()
            path_up.moveTo(cx - 3.5, cy - 5.0 + up_offset)
            path_up.lineTo(cx - 6.0, cy - 2.0 + up_offset)
            path_up.lineTo(cx - 4.5, cy - 2.0 + up_offset)
            path_up.lineTo(cx - 4.5, cy + 5.0 + up_offset)
            path_up.lineTo(cx - 2.5, cy + 5.0 + up_offset)
            path_up.lineTo(cx - 2.5, cy - 2.0 + up_offset)
            path_up.lineTo(cx - 1.0, cy - 2.0 + up_offset)
            path_up.closeSubpath()
            p.drawPath(path_up)
            
            # Right Down Arrow (Download)
            path_down = QPainterPath()
            path_down.moveTo(cx + 3.5, cy + 5.0 + down_offset)
            path_down.lineTo(cx + 1.0, cy + 2.0 + down_offset)
            path_down.lineTo(cx + 2.5, cy + 2.0 + down_offset)
            path_down.lineTo(cx + 2.5, cy - 5.0 + down_offset)
            path_down.lineTo(cx + 4.5, cy - 5.0 + down_offset)
            path_down.lineTo(cx + 4.5, cy + 2.0 + down_offset)
            path_down.lineTo(cx + 6.0, cy + 2.0 + down_offset)
            path_down.closeSubpath()
            p.drawPath(path_down)
        else:
            p.setPen(QPen(color, 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            
            # Left Up Arrow (Upload)
            p.drawLine(QPointF(cx - 3.5, cy + 5 + up_offset), QPointF(cx - 3.5, cy - 5 + up_offset))
            p.drawLine(QPointF(cx - 3.5, cy - 5 + up_offset), QPointF(cx - 5.5, cy - 2.5 + up_offset))
            p.drawLine(QPointF(cx - 3.5, cy - 5 + up_offset), QPointF(cx - 1.5, cy - 2.5 + up_offset))
            
            # Right Down Arrow (Download)
            p.drawLine(QPointF(cx + 3.5, cy - 5 + down_offset), QPointF(cx + 3.5, cy + 5 + down_offset))
            p.drawLine(QPointF(cx + 3.5, cy + 5 + down_offset), QPointF(cx + 1.5, cy + 2.5 + down_offset))
            p.drawLine(QPointF(cx + 3.5, cy + 5 + down_offset), QPointF(cx + 5.5, cy + 2.5 + down_offset))

    def paintEvent(self, event):
        # NET LED — Scheme: Cyan (download) / Violet (upload) / Slate (idle)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        if self.is_downloading:  core_color = QColor(0, 200, 220)   # cyan
        elif self.is_uploading:  core_color = QColor(160, 60, 230)   # violet
        else:                    core_color = QColor(80, 90, 100)     # slate

        pct = self.get_current_percentage()
        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 3. CPU Temperature Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class CPUTempWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "CPU Temperature Monitor")
        
        self.alert_wav_path = create_temp_wav("temp_alert", 880, 150)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        
        self.raw_temp = 40.0
        self.displayed_temp = 40.0
        self.top_proc_name = "System"
        self.top_proc_pct = 0.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 5.0
        
        self.monitor_worker = CPUTempWorker(interval_ms=50)
        self.monitor_worker.temp_updated.connect(self._on_temp_updated)
        self.monitor_worker.start()

    def _on_temp_updated(self, val, top_name="System", top_pct=0.0):
        self.raw_temp = val
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        self.setToolTip(f"CPU Temp: {self.displayed_temp:.1f}°C\nTop CPU: {top_name} ({top_pct:.1f}%)")
        if not self.isVisible(): return
        if self.parent_app.sound_enabled and val >= 78.0:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now
                self.sound_alert.play()

    def update_widget_state(self):
        if self.parent_app.transition_mode == "smooth":
            self.displayed_temp += (self.raw_temp - self.displayed_temp) * 0.15
        else:
            self.displayed_temp = self.raw_temp
        self.target_opacity = 0.5
        self.setToolTip(f"CPU Temp: {self.displayed_temp:.1f}°C\nTop CPU: {getattr(self, 'top_proc_name', 'System')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)")

    def get_current_percentage(self) -> float:
        return max(0.0, min(100.0, ((self.displayed_temp - 30.0) / 60.0) * 100.0))

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
        # CPU Temp — Scheme: Deep Blue (cool) → Amber (warm) → Orange-Red (hot)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = self.get_current_percentage()
        t = pct / 100.0
        if t < 0.5:
            core_color = self.lerp_color(QColor(30, 80, 200), QColor(230, 160, 20), t * 2.0)
        else:
            core_color = self.lerp_color(QColor(230, 160, 20), QColor(220, 50, 10), (t - 0.5) * 2.0)

        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 4. CPU Load Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class CPULoadWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "CPU Load Monitor")
        
        self.alert_wav_path = create_temp_wav("load_alert", 950, 120)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        
        self.raw_load = 0.0
        self.displayed_load = 0.0
        self.top_proc_name = "System"
        self.top_proc_pct = 0.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 4.0
        
        self.monitor_worker = CPULoadWorker(interval_ms=50)
        self.monitor_worker.load_updated.connect(self._on_load_updated)
        self.monitor_worker.start()

    def _on_load_updated(self, val, top_name="System", top_pct=0.0):
        self.raw_load = val
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        self.setToolTip(f"CPU Load: {self.displayed_load:.1f}%\nTop CPU: {top_name} ({top_pct:.1f}%)")
        if not self.isVisible(): return
        if self.parent_app.sound_enabled and val >= 92.0:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now
                self.sound_alert.play()

    def update_widget_state(self):
        if self.parent_app.transition_mode == "smooth":
            self.displayed_load += (self.raw_load - self.displayed_load) * 0.15
        else:
            self.displayed_load = self.raw_load
        self.target_opacity = 0.5
        self.setToolTip(f"CPU Load: {self.displayed_load:.1f}%\nTop CPU: {getattr(self, 'top_proc_name', 'System')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)")

    def get_current_percentage(self) -> float:
        return self.displayed_load

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # CPU Microchip icon
        if fill:
            p.setBrush(QBrush(color))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 1.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

        p.drawRoundedRect(QRectF(cx - 5.5, cy - 5.5, 11.0, 11.0), 1.5, 1.5)
        
        p.setPen(QPen(color, 1.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(QPointF(cx - 7.5, cy - 2.5), QPointF(cx - 5.5, cy - 2.5))
        p.drawLine(QPointF(cx - 7.5, cy + 2.5), QPointF(cx - 5.5, cy + 2.5))
        p.drawLine(QPointF(cx + 5.5, cy - 2.5), QPointF(cx + 7.5, cy - 2.5))
        p.drawLine(QPointF(cx + 5.5, cy + 2.5), QPointF(cx + 7.5, cy + 2.5))
        p.drawLine(QPointF(cx - 2.5, cy - 7.5), QPointF(cx - 2.5, cy - 5.5))
        p.drawLine(QPointF(cx + 2.5, cy - 7.5), QPointF(cx + 2.5, cy - 5.5))
        p.drawLine(QPointF(cx - 2.5, cy + 5.5), QPointF(cx - 2.5, cy + 7.5))
        p.drawLine(QPointF(cx + 2.5, cy + 5.5), QPointF(cx + 2.5, cy + 7.5))

        pulse_speed = 2.0 + (pct / 100.0) * 8.0
        pulse = 0.5 + 0.5 * math.sin(time.time() * pulse_speed)
        
        p.setPen(Qt.NoPen)
        core_c = QColor(255, 255, 255, int(150 + 105 * pulse)) if fill else QColor(color.red(), color.green(), color.blue(), int(150 + 105 * pulse))
        p.setBrush(QBrush(core_c))
        core_sz = 3.5 + 1.2 * pulse
        p.drawRect(QRectF(cx - core_sz/2.0, cy - core_sz/2.0, core_sz, core_sz))

    def paintEvent(self, event):
        # CPU Load — Scheme: Teal (idle) → Electric Blue (mid) → Cobalt (high)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = self.get_current_percentage()
        t = pct / 100.0
        if t < 0.5:
            core_color = self.lerp_color(QColor(0, 180, 160), QColor(30, 120, 255), t * 2.0)
        else:
            core_color = self.lerp_color(QColor(30, 120, 255), QColor(100, 30, 220), (t - 0.5) * 2.0)

        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 5. RAM Usage Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class RAMUsageWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "RAM Usage Monitor")
        
        self.alert_wav_path = create_temp_wav("ram_alert", 700, 150)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        
        self.raw_ram = 50.0
        self.displayed_ram = 50.0
        self.top_proc_name = "System"
        self.top_proc_pct = 0.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 10.0
        
        self.monitor_worker = RAMUsageWorker(interval_ms=50)
        self.monitor_worker.ram_updated.connect(self._on_ram_updated)
        self.monitor_worker.start()

    def _on_ram_updated(self, val, top_name="System", top_pct=0.0):
        self.raw_ram = val
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        self.setToolTip(f"RAM Usage: {self.displayed_ram:.1f}%\nTop RAM: {top_name} ({top_pct:.1f}%)")
        if not self.isVisible(): return
        if self.parent_app.sound_enabled and val >= 90.0:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now
                self.sound_alert.play()

    def update_widget_state(self):
        if self.parent_app.transition_mode == "smooth":
            self.displayed_ram += (self.raw_ram - self.displayed_ram) * 0.15
        else:
            self.displayed_ram = self.raw_ram
        self.target_opacity = 0.5
        self.setToolTip(f"RAM Usage: {self.displayed_ram:.1f}%\nTop RAM: {getattr(self, 'top_proc_name', 'System')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)")

    def get_current_percentage(self) -> float:
        return self.displayed_ram

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
        # RAM — Scheme: Indigo (low) → Magenta (mid) → Hot Pink (high)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = self.get_current_percentage()
        t = pct / 100.0
        if t < 0.5:
            core_color = self.lerp_color(QColor(80, 40, 200), QColor(200, 40, 180), t * 2.0)
        else:
            core_color = self.lerp_color(QColor(200, 40, 180), QColor(240, 60, 100), (t - 0.5) * 2.0)

        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Power Usage Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class PowerUsageWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "Power Usage Monitor")
        
        self.alert_wav_path = create_temp_wav("power_alert", 1200, 200)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        
        self.raw_watts = 20.0
        self.displayed_watts = 20.0
        self.top_cpu_name = "System"
        self.top_cpu_pct = 0.0
        self.top_gpu_name = "Idle"
        self.top_gpu_pct = 0.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 8.0
        
        self.monitor_worker = PowerWorker(interval_ms=50)
        self.monitor_worker.power_updated.connect(self._on_power_updated)
        self.monitor_worker.start()

    def _on_power_updated(self, val, top_cpu="System", top_cpu_p=0.0, top_gpu="Idle", top_gpu_p=0.0):
        self.raw_watts = val
        self.top_cpu_name = top_cpu
        self.top_cpu_pct = top_cpu_p
        self.top_gpu_name = top_gpu
        self.top_gpu_pct = top_gpu_p
        self.setToolTip(f"Power: {self.displayed_watts:.1f}W\nTop CPU: {top_cpu} ({top_cpu_p:.1f}%)\nTop GPU: {top_gpu} ({top_gpu_p:.1f}%)")
        if not self.isVisible(): return
        if self.parent_app.sound_enabled and val >= POWER_MAX * 0.85:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now
                self.sound_alert.play()

    def update_widget_state(self):
        if self.parent_app.transition_mode == "smooth":
            self.displayed_watts += (self.raw_watts - self.displayed_watts) * 0.15
        else:
            self.displayed_watts = self.raw_watts
        self.target_opacity = 0.5
        self.setToolTip(f"Power: {self.displayed_watts:.1f}W\nTop CPU: {getattr(self, 'top_cpu_name', 'System')} ({getattr(self, 'top_cpu_pct', 0.0):.1f}%)\nTop GPU: {getattr(self, 'top_gpu_name', 'Idle')} ({getattr(self, 'top_gpu_pct', 0.0):.1f}%)")

    def get_current_percentage(self) -> float:
        return max(0.0, min(100.0, (self.displayed_watts - POWER_MIN) / (POWER_MAX - POWER_MIN) * 100.0))

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # Lightning bolt icon
        pulse_speed = 1.5 + (pct / 100.0) * 6.0
        pulse = 0.5 + 0.5 * math.sin(time.time() * pulse_speed)
        bolt_alpha = int(160 + 95 * pulse)

        if fill:
            bolt_color = QColor(color.red(), color.green(), color.blue(), bolt_alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(bolt_color))
        else:
            p.setPen(QPen(color, 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)

        path = QPainterPath()
        path.moveTo(cx + 1.5, cy - 6.0)
        path.lineTo(cx - 2.5, cy - 0.5)
        path.lineTo(cx + 1.5, cy - 0.5)
        path.lineTo(cx - 1.5, cy + 6.0)
        path.lineTo(cx + 3.5, cy + 0.5)
        path.lineTo(cx - 0.5, cy + 0.5)
        path.closeSubpath()
        p.drawPath(path)

        if fill:
            gleam_pen = QPen(QColor(255, 255, 255, int(120 * pulse)), 0.6, Qt.SolidLine, Qt.RoundCap)
            p.setPen(gleam_pen)
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(cx + 0.5, cy - 4.5), QPointF(cx - 0.5, cy + 0.5))

    def paintEvent(self, event):
        # Power — Scheme: Forest Green (low) → Gold (mid) → Amber (high)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = self.get_current_percentage()
        t = pct / 100.0
        if t < 0.5:
            core_color = self.lerp_color(QColor(20, 160, 60), QColor(220, 190, 0), t * 2.0)
        else:
            core_color = self.lerp_color(QColor(220, 190, 0), QColor(220, 90, 10), (t - 0.5) * 2.0)

        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 7. GPU Temperature Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class GPUTempWidget(BaseMonitorWidget):
    def __init__(self, parent_app):
        super().__init__(parent_app, "GPU Temperature Monitor")
        
        self.alert_wav_path = create_temp_wav("gpu_temp_alert", 1050, 180)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        
        self.raw_dgpu_temp = 45.0
        self.raw_igpu_temp = 40.0
        self.raw_temp = 45.0
        self.displayed_temp = 45.0
        self.dgpu_name = "NVIDIA GeForce RTX 3050"
        self.igpu_name = "AMD Radeon(TM) Graphics"
        self.top_proc_name = "Idle"
        self.top_proc_pct = 0.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 6.0
        self._fan_angle = 0.0
        
        self.monitor_worker = GPUTempWorker(interval_ms=50)
        self.monitor_worker.temp_updated.connect(self._on_temp_updated)
        self.monitor_worker.start()

    def _on_temp_updated(self, dgpu_t, igpu_t, dgpu_n, igpu_n, top_name="Idle", top_pct=0.0):
        self.raw_dgpu_temp = dgpu_t
        self.raw_igpu_temp = igpu_t
        self.dgpu_name = dgpu_n
        self.igpu_name = igpu_n
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        self.raw_temp = max(dgpu_t, igpu_t)
        self.setToolTip(f"dGPU ({self.dgpu_name}): {self.raw_dgpu_temp:.0f}°C\niGPU ({self.igpu_name}): {self.raw_igpu_temp:.0f}°C\nTop GPU: {top_name} ({top_pct:.1f}%)")

        if not self.isVisible(): return
        if self.parent_app.sound_enabled and self.raw_temp >= 78.0:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now
                self.sound_alert.play()

    def update_widget_state(self):
        fan_rpm_pct = max(0.1, self.get_current_percentage() / 100.0)
        self._fan_angle = (self._fan_angle + 0.07 + fan_rpm_pct * 0.25) % (2 * math.pi)
        
        if self.parent_app.transition_mode == "smooth":
            self.displayed_temp += (self.raw_temp - self.displayed_temp) * 0.15
        else:
            self.displayed_temp = self.raw_temp
        self.target_opacity = 0.5
        self.setToolTip(f"dGPU ({self.dgpu_name}): {self.raw_dgpu_temp:.0f}°C\niGPU ({self.igpu_name}): {self.raw_igpu_temp:.0f}°C\nTop GPU: {getattr(self, 'top_proc_name', 'Idle')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)")

    def get_current_percentage(self) -> float:
        return max(0.0, min(100.0, (self.displayed_temp - 30.0) / 55.0 * 100.0))

    def draw_icon_graphics(self, p, cx, cy, pct, color, fill=False):
        # Rotating fan blades
        fan_r = 5.0
        hub_r = 1.4
        num_blades = 3

        if fill:
            p.setBrush(QBrush(color))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 1.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

        for blade_i in range(num_blades):
            base_angle = self._fan_angle + (2 * math.pi * blade_i / num_blades)
            path = QPainterPath()
            inner_a = base_angle
            outer_a = base_angle + 0.9

            x1 = cx + hub_r * math.cos(inner_a)
            y1 = cy + hub_r * math.sin(inner_a)
            x2 = cx + fan_r * math.cos(base_angle + 0.2)
            y2 = cy + fan_r * math.sin(base_angle + 0.2)
            x3 = cx + fan_r * math.cos(outer_a)
            y3 = cy + fan_r * math.sin(outer_a)
            x4 = cx + hub_r * math.cos(outer_a)
            y4 = cy + hub_r * math.sin(outer_a)

            path.moveTo(x1, y1)
            path.cubicTo(x2, y2, x3, y3, x3, y3)
            path.lineTo(x4, y4)
            path.closeSubpath()
            p.drawPath(path)

        if fill:
            p.setBrush(QBrush(color.lighter(120)))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color.lighter(120), 1.0))
        p.drawEllipse(QPointF(cx, cy), hub_r, hub_r)

    def paintEvent(self, event):
        # GPU Temp — Scheme: Sea Green (cool) → Lime (warm) → Red-Orange (hot)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = self.get_current_percentage()
        t = pct / 100.0
        if t < 0.5:
            core_color = self.lerp_color(QColor(20, 160, 100), QColor(180, 220, 30), t * 2.0)
        else:
            core_color = self.lerp_color(QColor(180, 220, 30), QColor(230, 60, 20), (t - 0.5) * 2.0)

        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# 8. GPU Usage Monitor Widget
# ─────────────────────────────────────────────────────────────────────────────

class GPUUsageWidget(BaseMonitorWidget):
    GRAPH_LEN = 16
    def __init__(self, parent_app):
        super().__init__(parent_app, "GPU Usage Monitor")
        
        self.alert_wav_path = create_temp_wav("gpu_use_alert", 800, 160)
        self.sound_alert = QSoundEffect(self)
        self.sound_alert.setSource(QUrl.fromLocalFile(self.alert_wav_path))
        
        self.raw_dgpu_usage = 0.0
        self.raw_igpu_usage = 0.0
        self.raw_total_usage = 0.0
        self.raw_usage = 0.0
        self.displayed_usage = 0.0
        self.dgpu_name = "NVIDIA GeForce RTX 3050"
        self.igpu_name = "AMD Radeon(TM) Graphics"
        self.top_proc_name = "Idle"
        self.top_proc_pct = 0.0
        self.last_beep_time = 0.0
        self.beep_cooldown = 6.0
        self._history = collections.deque([0.0] * self.GRAPH_LEN, maxlen=self.GRAPH_LEN)
        
        self.monitor_worker = GPUUsageWorker(interval_ms=50)
        self.monitor_worker.usage_updated.connect(self._on_usage_updated)
        self.monitor_worker.start()

    def _on_usage_updated(self, dgpu_u, igpu_u, total_u, dgpu_n, igpu_n, top_name="Idle", top_pct=0.0):
        self.raw_dgpu_usage = dgpu_u
        self.raw_igpu_usage = igpu_u
        self.raw_total_usage = total_u
        self.raw_usage = total_u
        self.dgpu_name = dgpu_n
        self.igpu_name = igpu_n
        self.top_proc_name = top_name
        self.top_proc_pct = top_pct
        self._history.append(self.raw_usage)
        self.setToolTip(f"dGPU ({self.dgpu_name}): {self.raw_dgpu_usage:.0f}%\niGPU ({self.igpu_name}): {self.raw_igpu_usage:.0f}%\nTotal GPU: {self.raw_total_usage:.0f}%\nTop GPU: {top_name} ({top_pct:.1f}%)")

        if not self.isVisible(): return
        if self.parent_app.sound_enabled and self.raw_usage >= 95.0:
            now = time.time()
            if now - self.last_beep_time > self.beep_cooldown:
                self.last_beep_time = now
                self.sound_alert.play()

    def update_widget_state(self):
        if self.parent_app.transition_mode == "smooth":
            self.displayed_usage += (self.raw_usage - self.displayed_usage) * 0.15
        else:
            self.displayed_usage = self.raw_usage
        self.target_opacity = 0.5
        self.setToolTip(f"dGPU ({self.dgpu_name}): {self.raw_dgpu_usage:.0f}%\niGPU ({self.igpu_name}): {self.raw_igpu_usage:.0f}%\nTotal GPU: {self.raw_total_usage:.0f}%\nTop GPU: {getattr(self, 'top_proc_name', 'Idle')} ({getattr(self, 'top_proc_pct', 0.0):.1f}%)")

    def get_current_percentage(self) -> float:
        return self.displayed_usage

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
        # GPU Usage — Scheme: Dark Teal (idle) → Emerald (mid) → Spring Green (maxed)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale_factor = self.parent_app.widget_size / 60.0
        p.scale(scale_factor, scale_factor)
        cx, cy = 30.0, 30.0
        r_base = 12.0

        pct = self.get_current_percentage()
        t = pct / 100.0
        if t < 0.5:
            core_color = self.lerp_color(QColor(0, 100, 100), QColor(0, 200, 120), t * 2.0)
        else:
            core_color = self.lerp_color(QColor(0, 200, 120), QColor(100, 255, 80), (t - 0.5) * 2.0)

        if self.parent_app.display_mode == 3:
            self.draw_animated_icon(p, cx, cy, pct, core_color)
        else:
            self.draw_core_led(p, cx, cy, r_base, core_color, pct)


# ─────────────────────────────────────────────────────────────────────────────
# Master Unified Application Class
# ─────────────────────────────────────────────────────────────────────────────

class VolumeSliderAction(QWidgetAction):
    def __init__(self, parent, initial_volume=50, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget()
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        self.lbl = QLabel("Volume:", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(0, 100)
        self.slider.setValue(initial_volume)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#ffaa00;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#ffaa00;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class SizeSliderAction(QWidgetAction):
    def __init__(self, parent, initial_size=60, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget()
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        self.lbl = QLabel("Size:  ", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(40, 120)
        self.slider.setValue(initial_size)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#ffaa00;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#ffaa00;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class OpacitySliderAction(QWidgetAction):
    def __init__(self, parent, initial_opacity=50, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget()
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        self.lbl = QLabel("Opacity:", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(10, 100)
        self.slider.setValue(initial_opacity)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#ffaa00;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#ffaa00;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class SpacingSliderAction(QWidgetAction):
    def __init__(self, parent, initial_spacing=4, callback=None):
        super().__init__(parent)
        self.callback = callback
        self.widget = QWidget()
        lay = QHBoxLayout(self.widget)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        self.lbl = QLabel("Spacing:", self.widget)
        self.lbl.setStyleSheet("color:#cccccc;font-size:10px;font-weight:bold;")
        lay.addWidget(self.lbl)
        self.slider = QSlider(Qt.Horizontal, self.widget)
        self.slider.setRange(0, 30)
        self.slider.setValue(initial_spacing)
        self.slider.setFixedWidth(80)
        self.slider.setStyleSheet("""
            QSlider{background:transparent;}
            QSlider::groove:horizontal{background:rgba(255,255,255,0.12);height:4px;border-radius:2px;}
            QSlider::sub-page:horizontal{background:#ffaa00;border-radius:2px;}
            QSlider::handle:horizontal{background:#fff;width:10px;height:10px;
                margin-top:-3px;margin-bottom:-3px;border-radius:5px;}
            QSlider::handle:horizontal:hover{background:#ffaa00;}
        """)
        self.slider.valueChanged.connect(lambda v: self.callback(v) if self.callback else None)
        lay.addWidget(self.slider)
        self.setDefaultWidget(self.widget)


class UnifiedMonitorApp(QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        self.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        self.setQuitOnLastWindowClosed(False)
        
        # Load configs
        self.config = load_config()
        self.is_locked = self.config.get("is_locked", False)
        self.click_through = self.config.get("click_through", False)
        self.display_mode = self.config.get("display_mode", 3)
        self.transition_mode = self.config.get("transition_mode", "smooth")
        self.sound_enabled = self.config.get("sound_enabled", True)
        self.volume = self.config.get("volume", 50)
        self.widget_size = self.config.get("widget_size", 60)
        self.base_opacity = self.config.get("base_opacity", 50)
        self.widget_spacing = self.config.get("widget_spacing", 4)
        self.alignment = self.config.get("alignment", "vertical")  # horizontal vs vertical
        
        # Restore base coordinates (defaults to bottom right corner)
        screen = self.primaryScreen().geometry()
        default_x = screen.width() - 80
        default_y = screen.height() - 550
        self.base_x = self.config.get("base_x", default_x)
        self.base_y = self.config.get("base_y", default_y)
        
        # Register startup if not done
        if not self.config.get("startup_registered", False):
            set_start_with_windows(True)
            self.config["startup_registered"] = True
            save_config(self.config)

        # Initialize widgets
        self.widget_hddled = HDDLEDWidget(self)
        self.widget_netled = NETLEDWidget(self)
        self.widget_cputemp = CPUTempWidget(self)
        self.widget_cpuload = CPULoadWidget(self)
        self.widget_ramusage = RAMUsageWidget(self)
        self.widget_powerusage = PowerUsageWidget(self)
        self.widget_gputemp = GPUTempWidget(self)
        self.widget_gpuusage = GPUUsageWidget(self)
        
        self.widgets = [
            self.widget_hddled,
            self.widget_netled,
            self.widget_cputemp,
            self.widget_cpuload,
            self.widget_ramusage,
            self.widget_powerusage,
            self.widget_gputemp,
            self.widget_gpuusage
        ]
        
        # IDs matching config
        self.widget_ids = {
            "hddled": self.widget_hddled,
            "netled": self.widget_netled,
            "cputemp": self.widget_cputemp,
            "cpuload": self.widget_cpuload,
            "ramusage": self.widget_ramusage,
            "powerusage": self.widget_powerusage,
            "gputemp": self.widget_gputemp,
            "gpuusage": self.widget_gpuusage
        }
        
        # Apply active states from config (defaults all to True)
        active_states = self.config.get("active_widgets", {k: True for k in self.widget_ids.keys()})
        for k, w in self.widget_ids.items():
            is_active = active_states.get(k, True)
            w.setVisible(is_active)
        
        # Set initial click through levels
        self.apply_click_through(self.click_through)
        
        # Sync volume levels
        self.apply_volume(self.volume)
        
        # Setup System Tray
        self._setup_tray()
        
        # Position all windows
        self.layout_widgets()
        
        # Tray dynamic updates timer (250ms)
        self.tray_timer = QTimer(self)
        self.tray_timer.timeout.connect(self._update_tray_state)
        self.tray_timer.start(250)

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.update_tray_icon()
        self.update_tray_tooltip()
        
        self.menu = QMenu()
        
        # Toggle Running Widgets Submenu
        toggle_menu = QMenu("Running Monitors", self.menu)
        active_states = self.config.get("active_widgets", {k: True for k in self.widget_ids.keys()})
        for name, label in [
            ("hddled", "HDD Activity LED"),
            ("netled", "Network Activity LED"),
            ("cputemp", "CPU Temperature"),
            ("cpuload", "CPU Load"),
            ("ramusage", "RAM Usage"),
            ("powerusage", "Power Usage"),
            ("gputemp", "GPU Temperature"),
            ("gpuusage", "GPU Usage")
        ]:
            act = QAction(label, self, checkable=True)
            act.setChecked(active_states.get(name, True))
            act.triggered.connect(lambda checked, n=name: self.toggle_widget(n, checked))
            toggle_menu.addAction(act)
            setattr(self, f"act_toggle_{name}", act)
            
        # Alignment Submenu
        align_menu = QMenu("Align Layout", self.menu)
        self.align_group = QActionGroup(self)
        self.act_align_h = QAction("Horizontal Row", self, checkable=True)
        self.act_align_h.setChecked(self.alignment == "horizontal")
        self.act_align_h.triggered.connect(lambda: self.change_alignment("horizontal"))
        
        self.act_align_v = QAction("Vertical Column", self, checkable=True)
        self.act_align_v.setChecked(self.alignment == "vertical")
        self.act_align_v.triggered.connect(lambda: self.change_alignment("vertical"))
        
        self.align_group.addAction(self.act_align_h)
        self.align_group.addAction(self.act_align_v)
        align_menu.addAction(self.act_align_h)
        align_menu.addAction(self.act_align_v)

        # Global Display Options
        mode_menu = QMenu("Display Style", self.menu)
        mode_group = QActionGroup(self)
        for val, lbl in [(1, "Flat Dot"), (2, "Activity Glow"), (3, "Animated Icon")]:
            a = QAction(lbl, self, checkable=True)
            a.setChecked(self.display_mode == val)
            a.triggered.connect(lambda _, x=val: self.change_display_mode(x))
            mode_group.addAction(a)
            mode_menu.addAction(a)
            setattr(self, f"act_style_{val}", a)

        trans_menu = QMenu("Color Transition Style", self.menu)
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

        # Standard context menu controls
        self.act_lock = QAction("Lock Group Position", self, checkable=True)
        self.act_lock.setChecked(self.is_locked)
        self.act_lock.triggered.connect(self.toggle_lock)
        
        self.act_click = QAction("Click Through", self, checkable=True)
        self.act_click.setChecked(self.click_through)
        self.act_click.triggered.connect(self.toggle_click_through)
        
        self.act_sound = QAction("Sound Alarms On", self, checkable=True)
        self.act_sound.setChecked(self.sound_enabled)
        self.act_sound.triggered.connect(self.toggle_sound)
        
        self.vol_action = VolumeSliderAction(self, self.volume, self.change_volume)
        self.size_action = SizeSliderAction(self, self.widget_size, self.change_widget_size)
        self.opacity_action = OpacitySliderAction(self, self.base_opacity, self.change_base_opacity)
        self.spacing_action = SpacingSliderAction(self, self.widget_spacing, self.change_widget_spacing)
        
        self.act_startup = QAction("Start with Windows", self, checkable=True)
        self.act_startup.setChecked(is_start_with_windows_enabled())
        self.act_startup.triggered.connect(self.toggle_startup)
        
        self.act_msinfo = QAction("System Information (msinfo32)", self)
        self.act_msinfo.triggered.connect(self.open_msinfo32)

        act_exit = QAction("Exit Monitors", self)
        act_exit.triggered.connect(self.exit_app)

        # Assemble menu
        self.menu.addMenu(toggle_menu)
        self.menu.addMenu(align_menu)
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
        self.menu.addAction(self.spacing_action)
        self.menu.addAction(self.act_startup)
        self.menu.addAction(self.act_msinfo)
        self.menu.addSeparator()
        self.menu.addAction(act_exit)
        
        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def open_msinfo32(self):
        import subprocess
        try:
            subprocess.Popen(["msinfo32.exe"], creationflags=0x08000000)
        except Exception as e:
            print("Failed to launch msinfo32:", e)

    def handle_widget_dragged(self, source_widget, new_pos):
        # Calculate delta shift relative to the widget's old position
        delta = new_pos - source_widget.pos()
        self.base_x += delta.x()
        self.base_y += delta.y()
        self.layout_widgets()

    def save_current_layout(self):
        # Anchor base_x and base_y to the first visible widget's position
        visible_widgets = [w for w in self.widgets if w.isVisible()]
        if visible_widgets:
            first_pos = visible_widgets[0].pos()
            self.base_x = first_pos.x()
            self.base_y = first_pos.y()
        self.save_config()

    def layout_widgets(self):
        visible_widgets = [w for w in self.widgets if w.isVisible()]
        if not visible_widgets:
            return
        cx, cy = self.base_x, self.base_y
        spacing = self.widget_spacing
        
        for w in visible_widgets:
            w.move(cx, cy)
            if self.alignment == "horizontal":
                cx += w.width() + spacing
            else:
                cy += w.height() + spacing

    def toggle_widget(self, name, checked):
        w = self.widget_ids[name]
        w.setVisible(checked)
        self.layout_widgets()
        
        # Save active states
        active_states = self.config.get("active_widgets", {k: True for k in self.widget_ids.keys()})
        active_states[name] = checked
        self.config["active_widgets"] = active_states
        self.save_config()
        self.update_tray_tooltip()

    def change_alignment(self, mode):
        self.alignment = mode
        self.act_align_h.setChecked(mode == "horizontal")
        self.act_align_v.setChecked(mode == "vertical")
        self.layout_widgets()
        self.config["alignment"] = mode
        self.save_config()

    def change_display_mode(self, mode):
        self.display_mode = mode
        for idx in [1, 2, 3]:
            getattr(self, f"act_style_{idx}").setChecked(mode == idx)
        for w in self.widgets:
            w.update()
        self.config["display_mode"] = mode
        self.save_config()

    def change_transition(self, mode):
        self.transition_mode = mode
        self.act_trans_smooth.setChecked(mode == "smooth")
        self.act_trans_snappy.setChecked(mode == "snappy")
        self.config["transition_mode"] = mode
        self.save_config()

    def toggle_lock(self, checked):
        self.is_locked = checked
        self.act_lock.setChecked(checked)
        self.config["is_locked"] = checked
        self.save_config()

    def toggle_click_through(self, checked):
        self.click_through = checked
        self.act_click.setChecked(checked)
        self.apply_click_through(checked)
        self.config["click_through"] = checked
        self.save_config()

    def apply_click_through(self, checked):
        for w in self.widgets:
            w.hide()
            flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.SubWindow
            if checked:
                flags |= Qt.WindowTransparentForInput
            w.setWindowFlags(flags)
            if w.parent_app.config.get("active_widgets", {k: True for k in self.widget_ids.keys()}).get(
                [k for k, val in self.widget_ids.items() if val == w][0], True
            ):
                w.show()
        self.layout_widgets()

    def toggle_sound(self, checked):
        self.sound_enabled = checked
        self.act_sound.setChecked(checked)
        self.config["sound_enabled"] = checked
        self.save_config()

    def change_volume(self, val):
        self.volume = val
        self.apply_volume(val)
        self.config["volume"] = val
        self.save_config()

    def apply_volume(self, val):
        vol_fraction = val / 100.0
        # Sync all sound generators
        self.widget_hddled.sound_read.setVolume(vol_fraction)
        self.widget_hddled.sound_write.setVolume(vol_fraction)
        self.widget_netled.sound_down.setVolume(vol_fraction)
        self.widget_netled.sound_up.setVolume(vol_fraction)
        self.widget_cputemp.sound_alert.setVolume(vol_fraction)
        self.widget_cpuload.sound_alert.setVolume(vol_fraction)
        self.widget_ramusage.sound_alert.setVolume(vol_fraction)
        self.widget_powerusage.sound_alert.setVolume(vol_fraction)
        self.widget_gputemp.sound_alert.setVolume(vol_fraction)
        self.widget_gpuusage.sound_alert.setVolume(vol_fraction)

    def toggle_startup(self, checked):
        set_start_with_windows(checked)
        self.act_startup.setChecked(checked)

    def change_widget_size(self, val):
        self.widget_size = val
        self.apply_widget_size(val)
        self.config["widget_size"] = val
        self.save_config()

    def apply_widget_size(self, val):
        for w in self.widgets:
            w.setFixedSize(val, val)
        self.layout_widgets()

    def change_widget_spacing(self, val):
        self.widget_spacing = val
        self.layout_widgets()
        self.config["widget_spacing"] = val
        self.save_config()

    def change_base_opacity(self, val):
        self.base_opacity = val
        self.apply_base_opacity(val)
        self.config["base_opacity"] = val
        self.save_config()

    def apply_base_opacity(self, val):
        for w in self.widgets:
            w.update()

    def save_config(self):
        self.config["base_x"] = self.base_x
        self.config["base_y"] = self.base_y
        self.config["is_locked"] = self.is_locked
        self.config["click_through"] = self.click_through
        self.config["display_mode"] = self.display_mode
        self.config["transition_mode"] = self.transition_mode
        self.config["sound_enabled"] = self.sound_enabled
        self.config["volume"] = self.volume
        self.config["widget_size"] = self.widget_size
        self.config["base_opacity"] = self.base_opacity
        self.config["widget_spacing"] = self.widget_spacing
        self.config["alignment"] = self.alignment
        save_config(self.config)

    def _update_tray_state(self):
        self.update_tray_icon()
        self.update_tray_tooltip()

    def update_tray_icon(self):
        max_pct = 0.0
        for w in self.widgets:
            if w.isVisible():
                max_pct = max(max_pct, w.get_current_percentage())
                
        # Color based on maximum metric load (gradient green -> orange/yellow -> red)
        # Re-using get_color_for_percentage mapping logic
        if self.transition_mode == "snappy":
            if max_pct < 40.0:   c = QColor(0, 230, 100)
            elif max_pct < 75.0: c = QColor(255, 170, 0)
            else:                c = QColor(255, 45, 45)
        else:
            t = max_pct / 100.0
            if t < 0.5:
                s = t * 2.0
                c = QColor(int(240*s), int(230+(180-230)*s), int(100-100*s))
            else:
                s = (t - 0.5) * 2.0
                c = QColor(int(240+15*s), int(180+(45-180)*s), int(45*s))
                
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        
        # Draw miniature monitor outline
        p.setPen(QPen(QColor(255, 255, 255, 200), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(1, 1, 14, 11), 1, 1)
        p.drawLine(QPointF(5, 13), QPointF(11, 13))
        p.drawLine(QPointF(8, 12), QPointF(8, 13))
        
        # Fill mini screen with status color
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(c))
        p.drawRect(QRectF(2.5, 2.5, 11, 8))
        p.end()
        
        self.tray.setIcon(QIcon(pix))

    def update_tray_tooltip(self):
        parts = []
        if self.widget_cputemp.isVisible():
            top_c = getattr(self.widget_cputemp, 'top_proc_name', 'System')
            parts.append(f"CPU: {self.widget_cputemp.displayed_temp:.0f}°C (Top: {top_c})")
        if self.widget_cpuload.isVisible():
            top_c = getattr(self.widget_cpuload, 'top_proc_name', 'System')
            parts.append(f"CPU Load: {self.widget_cpuload.displayed_load:.0f}% (Top: {top_c})")
        if self.widget_ramusage.isVisible():
            top_r = getattr(self.widget_ramusage, 'top_proc_name', 'System')
            parts.append(f"RAM: {self.widget_ramusage.displayed_ram:.0f}% (Top: {top_r})")
        if self.widget_powerusage.isVisible():
            parts.append(f"Power: {self.widget_powerusage.displayed_watts:.0f}W")
        if self.widget_gputemp.isVisible():
            top_g = getattr(self.widget_gputemp, 'top_proc_name', 'Idle')
            parts.append(f"dGPU: {self.widget_gputemp.raw_dgpu_temp:.0f}°C | iGPU: {self.widget_gputemp.raw_igpu_temp:.0f}°C (Top: {top_g})")
        if self.widget_gpuusage.isVisible():
            top_g = getattr(self.widget_gpuusage, 'top_proc_name', 'Idle')
            parts.append(f"GPU Load: {self.widget_gpuusage.raw_total_usage:.0f}% (Top: {top_g})")
            
        if parts:
            tooltip = "System Monitors\n" + "\n".join(parts)
        else:
            tooltip = "System Monitors (All Hidden)"
            
        self.tray.setToolTip(tooltip[:127])

    def exit_app(self):
        # Stop all workers
        self.widget_hddled.monitor_worker.stop(); self.widget_hddled.monitor_worker.wait()
        self.widget_netled.monitor_worker.stop(); self.widget_netled.monitor_worker.wait()
        self.widget_cputemp.monitor_worker.stop(); self.widget_cputemp.monitor_worker.wait()
        self.widget_cpuload.monitor_worker.stop(); self.widget_cpuload.monitor_worker.wait()
        self.widget_ramusage.monitor_worker.stop(); self.widget_ramusage.monitor_worker.wait()
        self.widget_powerusage.monitor_worker.stop(); self.widget_powerusage.monitor_worker.wait()
        self.widget_gputemp.monitor_worker.stop(); self.widget_gputemp.monitor_worker.wait()
        self.widget_gpuusage.monitor_worker.stop(); self.widget_gpuusage.monitor_worker.wait()
        self.quit()


if __name__ == "__main__":
    app = UnifiedMonitorApp(sys.argv)
    sys.exit(app.exec_())
