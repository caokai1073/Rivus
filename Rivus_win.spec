# -*- mode: python ; coding: utf-8 -*-
# Rivus_win.spec — PyInstaller 打包配置（Windows 专用）

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ui', 'ui'),                  # 前端 HTML/CSS/JS
    ],
    hiddenimports=[
        # uvicorn
        'uvicorn.logging',
        'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.on',
        # fastapi / starlette
        'fastapi', 'starlette', 'starlette.routing',
        'multipart', 'python_multipart',
        # embedding
        'sentence_transformers', 'transformers', 'tokenizers',
        'torch', 'numpy', 'sklearn', 'scipy', 'PIL',
        # db
        'sqlite_vec', 'sqlite3',
        # 文档解析
        'readability', 'lxml', 'lxml.etree', 'lxml.html',
        'fitz',                        # pymupdf
        'docx',                        # python-docx
        # 网络
        'requests', 'certifi', 'charset_normalizer',
        # pywebview
        'webview', 'webview.platforms.winforms',
        'clr', 'pythoncom', 'win32api', 'win32con',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'IPython', 'jupyter', 'notebook',
        'PyQt5', 'PyQt6', 'tkinter', 'wx',
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
    [],
    exclude_binaries=True,
    name='Rivus',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # 不显示黑色命令行窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='AppIcon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Rivus',
)
