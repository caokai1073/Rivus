#!/bin/bash
# 从 ui/icon.png 生成 AppIcon.icns，只需运行一次
set -e
cd "$(dirname "$0")"

TMP=$(mktemp -d)
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"

python3 - <<EOF
from PIL import Image
src = "ui/icon.png"
sizes = [16, 32, 64, 128, 256, 512, 1024]
img = Image.open(src).convert("RGBA")
for s in sizes:
    img.resize((s, s), Image.LANCZOS).save(f"$ICONSET/icon_{s}x{s}.png")
    if s <= 512:
        img.resize((s*2, s*2), Image.LANCZOS).save(f"$ICONSET/icon_{s}x{s}@2x.png")
print("图片生成完毕")
EOF

iconutil -c icns "$ICONSET" -o AppIcon.icns
rm -rf "$TMP"
echo "✅ AppIcon.icns 已生成"
