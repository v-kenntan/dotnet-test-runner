# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None

base_dir = os.path.dirname(os.path.abspath(SPEC))
backend_dir = os.path.join(base_dir, 'backend')

a = Analysis(
    [os.path.join(backend_dir, 'app.py'),
     os.path.join(backend_dir, 'executor.py')],
    pathex=[backend_dir],
    binaries=[],
    datas=[
        (os.path.join(backend_dir, 'test_definitions'), 'test_definitions'),
        (os.path.join(backend_dir, 'static'), 'static'),
    ],
    hiddenimports=[
        'flask',
        'flask_cors',
        'yaml',
        'sqlite3',
        'executor',
        'webview',
        'webview.platforms',
        'webview.platforms.gtk',
        'webview.platforms.cocoa',
        'webview.platforms.edgechromium',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='dotnet-test-runner',
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
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='dotnet-test-runner',
)
