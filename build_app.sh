#!/bin/bash
# build_app.sh — 打包 Rivus.app 并生成 DMG
set -e

APP="Rivus.app.nosync"
VERSION="1.0.0"
OUT="dist.nosync/Rivus-${VERSION}.dmg"
TMP="dist.nosync/Rivus-tmp.dmg"

echo "=== 0. 弹出已挂载的 Rivus 卷 ==="
hdiutil info | grep -o '/Volumes/Rivus[^"]*' | while read vol; do
    echo "   弹出: $vol"
    hdiutil detach "$vol" -force 2>/dev/null || true
done

echo "=== 1. 复制源码到 .app/Resources ==="
DEST="${APP}/Contents/Resources/app"
rm -rf "$DEST"
mkdir -p "$DEST"
cp app.py server.py db.py ingest.py query.py config.py requirements.txt "$DEST/"
cp -r ui "$DEST/"

echo "=== 1.5. 写入 Info.plist 和 AppIcon.icns ==="
cp Info.plist "${APP}/Contents/Info.plist"
if [ -f AppIcon.icns ]; then
    cp AppIcon.icns "${APP}/Contents/Resources/AppIcon.icns"
    echo "   AppIcon.icns 已写入"
else
    echo "   ⚠️  AppIcon.icns 不存在，请先运行 bash make_icns.sh"
fi

echo "=== 2. 设置可执行权限 ==="
chmod +x "${APP}/Contents/MacOS/Rivus"

echo "=== 2.5. Ad-hoc 签名（降低 Gatekeeper 警告级别）==="
# 先移除所有已有签名和隔离属性，再重新签名
xattr -cr "${APP}" 2>/dev/null || true
codesign --force --deep --sign - \
    --entitlements /dev/null \
    --options runtime \
    "${APP}" 2>/dev/null || \
codesign --force --deep --sign - "${APP}"
echo "   签名完成"

echo "=== 3. 创建 DMG ==="
mkdir -p dist.nosync
rm -f "$TMP"
trap 'rm -f "$TMP"' EXIT

hdiutil create -size 50m -fs HFS+ -volname "Rivus" -o "$TMP"
MOUNT_DIR=$(hdiutil attach "$TMP" | tail -1 | cut -f3)
echo "   挂载到: $MOUNT_DIR"

cp -r "$APP" "$MOUNT_DIR/Rivus.app"
ln -sf /Applications "$MOUNT_DIR/Applications"


# 写安装说明
cat > "$MOUNT_DIR/安装说明 · Installation Guide.txt" << 'EOF'
首次安装说明
============

1. 将 Rivus 拖入 Applications 文件夹
2. 双击 Rivus 尝试打开，macOS 会弹出警告窗口
3. 点击「好」或「完成」关闭警告（不要点"移到废纸篓"）
4. 打开「系统设置」→「隐私与安全性」，向下滚动
   找到「"Rivus"已被阻止…」的提示，点击「仍要打开」
5. 再次确认打开，此后正常双击启动即可，不再提示

关于本地 AI 模型
----------------
使用本地模型功能需要安装 Ollama。
首次设置时，问渠会尝试自动下载并安装 Ollama，
macOS 可能会弹出权限请求，请点击「允许」。

如果自动安装失败，请前往 Ollama 官网手动下载：
  https://ollama.com


Installation Guide
==================

1. Drag Rivus into the Applications folder
2. Double-click Rivus to open — macOS will show a warning dialog
3. Click "OK" or "Done" to dismiss it (do NOT click "Move to Trash")
4. Open System Settings → Privacy & Security, scroll down
   Find the '"Rivus" was blocked' notice and click "Open Anyway"
5. Confirm once more — from then on, double-click to launch as normal

About Local AI Models
---------------------
Running local models requires Ollama to be installed.
During first-time setup, Rivus will attempt to download and install
Ollama automatically. macOS may prompt for permissions — please click Allow.

If automatic installation fails, download Ollama manually from:
  https://ollama.com
EOF

hdiutil detach "$MOUNT_DIR" -force
hdiutil convert "$TMP" -format UDZO -o "$OUT" -ov

echo ""
echo "✅ 完成: $OUT"
