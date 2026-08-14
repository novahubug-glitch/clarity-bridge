# -*- mode: python ; coding: utf-8 -*-
# Builds ClarityBridge.exe as a single-file console executable.
# Console stays visible (not windowed) — the first run needs to show the
# pairing-code prompt, and ongoing logs are genuinely useful during testing.

block_cipher = None

from PyInstaller.utils.hooks import collect_all

# hiddenimports alone tells PyInstaller a module exists — it does NOT pull
# in a package's binaries/data files. numpy and MetaTrader5 both ship
# compiled extensions, so they need the fuller collect_all() treatment.
# (This was the root cause of every shipped build missing numpy.)
numpy_datas, numpy_binaries, numpy_hiddenimports = collect_all('numpy')
mt5_datas, mt5_binaries, mt5_hiddenimports = collect_all('MetaTrader5')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=numpy_binaries + mt5_binaries,
    datas=numpy_datas + mt5_datas,
    hiddenimports=numpy_hiddenimports + mt5_hiddenimports + ['MetaTrader5', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ClarityBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
