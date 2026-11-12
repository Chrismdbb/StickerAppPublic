import sys
import numpy as np
import cv2
from PIL import Image

K, S = 3.3866668, 1.01333
W, H = 1200, 2100            # printer sheet: 4x7 in at 300 dpi
SIDE, TOP = 40, 60           # blade-safe margins for every cut point
OFFSET = 20                  # cut line sits this many px outside the artwork
WHITE = 235                  # pixels brighter than this count as background
MIN_AREA = 3000              # ignore specks smaller than this


def to_plt(x, y):
    return round(((H - y) * S - 14.0) * K), round((x * S - 8.0) * K)


args = [a for a in sys.argv[1:] if not a.startswith("--")]
rect_mode = "--rect" in sys.argv
if len(args) != 2:
    sys.exit("usage: python3 make_sheet2.py <image> <output-name> [--rect]")
src, out = args

img = Image.open(src).convert("RGBA")
bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
bg.alpha_composite(img)
img = bg.convert("RGB")
w, h = img.size
if abs(w / h - W / H) > 0.01 * (W / H):
    sys.exit(f"Image {w}x{h} is not a 4x7 shape (expected about {W}x{H})")
if (w, h) != (W, H):
    img = img.resize((W, H), Image.LANCZOS)
    print(f"Resized {w}x{h} -> {W}x{H}")

arr = np.array(img)
mask = (arr.min(axis=2) < WHITE).astype(np.uint8) * 255
kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * OFFSET + 1, 2 * OFFSET + 1))
mask = cv2.dilate(mask, kernel)
found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
found = [c for c in found if cv2.contourArea(c) >= MIN_AREA]
if not found:
    sys.exit("No artwork found on the sheet")

shapes = []
for c in found:
    if rect_mode:
        x, y, bw, bh = cv2.boundingRect(c)
        pts = [(x, y), (x + bw, y), (x + bw, y + bh), (x, y + bh)]
    else:
        pts = [tuple(p[0]) for p in cv2.approxPolyDP(c, 1.5, True)]
    shapes.append(pts)
shapes.sort(key=lambda s: min(p[1] for p in s))

for i, pts in enumerate(shapes, 1):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if min(xs) < SIDE or max(xs) > W - SIDE or min(ys) < TOP or max(ys) > H - TOP:
        sys.exit(f"Sticker {i} reaches the unsafe sheet edge "
                 f"(x {min(xs)}-{max(xs)}, y {min(ys)}-{max(ys)}). Move artwork inward.")

cmds = []
for pts in shapes:
    pts = pts + [pts[0]]
    p = [to_plt(x, y) for x, y in pts]
    cmds.append(f"U{p[0][0]},{p[0][1]}")
    cmds += [f"D{a},{b}" for a, b in p]
cmds.append("U0,0 @")

img.save(out + ".jpg", quality=95)
open(out + ".plt", "w").write("IN VER0.1.0 KP42 " + " ".join(cmds))
print(f"{len(shapes)} sticker(s), {sum(len(s) for s in shapes)} cut points "
      f"({'rectangles' if rect_mode else 'die-cut contours'})")
print(f"wrote {out}.jpg and {out}.plt")
