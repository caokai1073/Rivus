"""
make_ico.py — 从 ui/icon.png 生成 AppIcon.ico（Windows 图标）
在 Windows 上运行一次即可：python make_ico.py
"""
from PIL import Image

img = Image.open("ui/icon.png").convert("RGBA")
img.save("AppIcon.ico", format="ICO",
         sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("✅ AppIcon.ico 生成完成")
