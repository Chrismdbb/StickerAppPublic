import sys
from PIL import Image

K, S = 3.3866668, 1.01333
W, H = 1200, 2100
SIDE, TOP = 40, 60          # keep the cut away from the sheet edges


def to_plt(x, y):
    a = ((H - y) * S - 14.0) * K
    b = (x * S - 8.0) * K
    return round(a), round(b)


src, out = sys.argv[1], sys.argv[2]
img = Image.open(src)
if img.mode in ("RGBA", "LA", "P"):
    bg = Image.new("RGB", img.size, "white")
    img = img.convert("RGBA")
    bg.paste(img, mask=img.split()[-1])
    img = bg
else:
    img = img.convert("RGB")
w, h = img.size
if w > W - 2 * SIDE or h > H - 2 * TOP:
    sys.exit(f"Image {w}x{h} too big for the {W}x{H} sheet (max {W - 2 * SIDE}x{H - 2 * TOP})")

x0 = int(sys.argv[3]) if len(sys.argv) > 3 else (W - w) // 2
y0 = int(sys.argv[4]) if len(sys.argv) > 4 else TOP
x1, y1 = x0 + w, y0 + h
if x0 < SIDE or x1 > W - SIDE or y0 < TOP or y1 > H - TOP:
    sys.exit("Placement is too close to the sheet edge")

sheet = Image.new("RGB", (W, H), "white")
sheet.paste(img, (x0, y0))
sheet.save(out + ".jpg", quality=95)

pts = [to_plt(x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
cmds = [f"U{pts[0][0]},{pts[0][1]}"] + [f"D{a},{b}" for a, b in pts] + ["U0,0 @"]
open(out + ".plt", "w").write("IN VER0.1.0 KP42 " + " ".join(cmds))
print(f"sheet: {out}.jpg  cut: {out}.plt  image placed at ({x0},{y0})-({x1},{y1})")
