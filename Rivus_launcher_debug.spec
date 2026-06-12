# -*- mode: python ; coding: utf-8 -*-
# Rivus_launcher_debug.spec — DEBUG BUILD: console=True, upx=False
# Use this to diagnose startup issues. Build with:
#   pyinstaller Rivus_launcher_debug.spec --clean --noconfirm

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app.py',           'app'),
        ('server.py',        'app'),
        ('db.py',            'app'),
        ('ingest.py',        'app'),
        ('query.py',         'app'),
        ('config.py',        'app'),
        ('remote.py',        'app'),
        ('requirements.txt', 'app'),
        ('AppIcon.ico',      'app'),
        ('ui',               'app/ui'),
    ],
    hiddenimports=[
        'tkinter', 'tkinter.ttk',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'torch', 'transformers', 'sentence_transformers',
        'fastapi', 'uvicorn', 'starlette',
        'numpy', 'scipy', 'sklearn',
        'sqlite_vec', 'fitz', 'docx',
        'readability', 'lxml',
        'webview',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Rivus_debug',
    debug=True,       # verbose PyInstaller bootloader output
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # disable UPX (common cause of silent crashes / AV false positives)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,     # show a console window with all print/traceback output
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='AppIcon.ico',
)
