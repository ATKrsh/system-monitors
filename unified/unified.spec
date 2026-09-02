# -*- mode: python ; coding: utf-8 -*-

# Unused heavy packages to exclude (not needed by this widget)
_EXCLUDES = [
    'numpy', 'numpy.core', 'numpy.linalg', 'numpy.fft', 'numpy.random',
    'matplotlib', 'matplotlib.backends', 'matplotlib.figure',
    'scipy', 'scipy.linalg', 'scipy.fft',
    'torch', 'torchvision',
    'PIL', 'Pillow',
    'cv2',
    'ultralytics',
    'pandas', 'polars',
    'sklearn', 'skimage',
    'sympy', 'mpmath',
    'tkinter', '_tkinter',
    'wx',
    'PyQt5.QtWebEngine', 'PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebEngineCore',
    'PyQt5.QtQml', 'PyQt5.QtQuick', 'PyQt5.QtQuickWidgets',
    'PyQt5.QtSql', 'PyQt5.QtXml', 'PyQt5.QtXmlPatterns',
    'PyQt5.QtTest', 'PyQt5.QtHelp', 'PyQt5.QtOpenGL',
    'PyQt5.QtBluetooth', 'PyQt5.QtPositioning', 'PyQt5.QtSensors',
    'PyQt5.QtNfc', 'PyQt5.QtWebChannel', 'PyQt5.QtWebSockets',
    'PyQt5.QtSvg', 'PyQt5.QtDesigner', 'PyQt5.QtLocation',
    'PyQt5.QtTextToSpeech', 'PyQt5.QtRemoteObjects',
    'PyQt5.Qt3DCore', 'PyQt5.Qt3DRender', 'PyQt5.Qt3DAnimation',
    'PyQt5.Qt3DExtras', 'PyQt5.Qt3DInput', 'PyQt5.Qt3DLogic',
    'PyQt6', 'pyqtgraph',
    'winrt',
]

_EXCLUDE_DLLS = {
    'opengl32sw.dll', 'Qt5Qml.dll', 'Qt5QmlModels.dll', 'Qt5Quick.dll', 'Qt5Svg.dll', 'Qt5DBus.dll', 'Qt5WebSockets.dll', 'qwebgl.dll', 'qtuiotouchplugin.dll', 'qxdgdesktopportal.dll', 'qoffscreen.dll', 'qminimal.dll'
}

a = Analysis(
    ['unified.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

# Filter out large unused DLLs
a.binaries = [b for b in a.binaries
              if not any(b[0].lower().endswith(dll.lower()) or
                         b[0].lower() == dll.lower()
                         for dll in _EXCLUDE_DLLS)]

# Drop all Qt translation files
a.datas = [d for d in a.datas
           if not (d[0].startswith('PyQt5\\Qt5\\translations\\') or
                   d[0].startswith('PyQt5/Qt5/translations/'))]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='unified',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
