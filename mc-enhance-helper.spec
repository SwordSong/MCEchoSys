# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('models\\yolov8_custom.onnx', 'models'), ('data\\echo_dictionary.json', 'data'), ('data\\panel_layout.json', 'data'), ('data\\strategy_priority.json', 'data'), ('data\\substat_values.json', 'data'), ('data\\character_name_entry_id.json', 'data')]
binaries = []
hiddenimports = ['cv2', 'dxcam', 'windows_capture', 'PyQt6.sip', 'sqlcipher3', 'sqlcipher3.dbapi2', 'pysqlcipher3.dbapi2']
tmp_ret = collect_all('rapidocr_onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['src\\ui\\overlay.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['ultralytics', 'torch', 'torchvision', 'torchaudio'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='mc-enhance-helper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='mc-enhance-helper',
)
