# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller のビルド定義 (onefile / ウィンドウ表示)。

`py -3.12 -m PyInstaller --noconfirm --clean m3u8dl.spec` でビルドする。
生成物は dist/m3u8dl.exe。
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # AES の高速バックエンド (pycryptodome) を確実に同梱する
    hiddenimports=['Crypto', 'Crypto.Cipher.AES', 'Crypto.Util._counter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 使わない重量級パッケージを除外して exe サイズを抑える
    excludes=[
        'cryptography',
        'numpy',
        'PIL',
        'matplotlib',
        'pandas',
        'pytest',
        'setuptools',
        'unittest',
        'test',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='m3u8dl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # コンソールを出さない GUI アプリにする (--selftest は JSON ファイルに出力する)
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
