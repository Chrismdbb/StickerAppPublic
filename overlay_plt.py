import re, sys
from PIL import Image, ImageDraw

K, S = 3.3866668, 1.01333   # constants from the Bluetooth repo's README


def to_px(a, b):
    return ((b / K + 8.0) / S, 2100 - (a / K + 14.0) / S)   # (x, y), y flipped


plt_path, img_path, out_path = sys.argv[1:4]
txt = open(plt_path).read()

strokes, cur = [], []
for cmd, a, b in re.findall(r"([UD])(-?\d+),(-?\d+)", txt):
    a, b = int(a), int(b)
    if cmd == "U":
        if (a, b) == (0, 0):
            continue
        if cur:
            strokes.append(cur)
        cur = [to_px(a, b)]
    else:
        cur.append(to_px(a, b))
if cur:
    strokes.append(cur)

img = Image.open(img_path).convert("RGB")
draw = ImageDraw.Draw(img)
allpts = [p for s in strokes for p in s]
for s in strokes:
    if len(s) > 1:
        draw.line(s, fill=(255, 0, 0), width=4)
img.save(out_path)

xs = [p[0] for p in allpts]
ys = [p[1] for p in allpts]
print(f"image: {img.size[0]}x{img.size[1]} px")
print(f"strokes: {len(strokes)}, points: {len(allpts)}")
print(f"cut bounds (px): x {min(xs):.0f}-{max(xs):.0f}, y {min(ys):.0f}-{max(ys):.0f}")
print("saved", out_path)
