# -*- mode: python ; coding: utf-8 -*-
# Rivus_launcher.spec — 轻量启动器（不打包 ML 依赖，首次运行时自动安装）

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 把全部源文件打进去，启动器会在首次运行时解压到 AppData
        ('app.py',           'app'),
        ('server.py',        'app'),
        ('db.py',            'app'),
        ('ingest.py',        'app'),
        ('query.py',         'app'),
        ('config.py',        'app'),
        ('requirements.txt', 'app'),
        ('ui',               'app/ui'),
    ],
    hiddenimports=[
        'tkinter', 'tkinter.ttk',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 故意排除所有重型包，让 pip 在用户机器上安装
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
    name='Rivus',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='AppIcon.ico',
    # onefile=True，单个 exe 文件，方便分发
)
