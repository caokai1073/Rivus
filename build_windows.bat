@echo off
REM build_windows.bat — 打包 Rivus Windows 版
REM 在 Windows 上运行：双击或在命令行执行
setlocal

echo === 0. 检查 Python 环境 ===
python --version || (echo ❌ 未找到 Python & pause & exit /b 1)

echo === 1. 安装/升级依赖 ===
pip install -r requirements.txt pyinstaller pillow --quiet

echo === 2. 生成 Windows 图标 ===
python make_ico.py || (echo ❌ 图标生成失败 & pause & exit /b 1)

echo === 3. PyInstaller 打包 ===
pyinstaller Rivus_win.spec --clean --noconfirm
if errorlevel 1 (
    echo ❌ 打包失败，查看上方报错
    pause
    exit /b 1
)

echo === 4. 打包完成 ===
echo ✅ 输出目录：dist\Rivus\
echo    可将整个 dist\Rivus\ 文件夹压缩后分发给用户
echo    或使用 NSIS / Inno Setup 进一步制作安装包
pause
