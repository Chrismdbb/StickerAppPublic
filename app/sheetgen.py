"""Turn a booth image into a printer-ready sheet (1200x2100 JPEG) plus a cut file.

The cut-file maths was verified against a file the S1 cut correctly (note the vertical
flip). Defaults reproduce exactly what make_sheet2.py produced for the first
successful die-cut test sticker.
"""
from PIL import Image, ImageDraw, ImageOps

K, S = 3.3866668, 1.01333
W, H = 1200, 2100          # 4x7 inch sheet at 300 dpi
SIDE, TOP = 40, 60         # the blade cannot cut reliably near the sheet edges
MIN_AREA = 3000


class SheetError(Exception):
    pass


def to_plt(x, y):
    return round(((H - y) * S - 14.0) * K), round((x * S - 8.0) * K)


def _load_flat(path):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im) or im
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    return bg.convert("RGB")


def _to_sheet(img, policy):
    w, h = img.size
    if w < 2 or h < 2:
        raise SheetError(f"Image is too small ({w}x{h})")
    if abs(w / h - W / H) <= 0.01 * (W / H):
        if (w, h) != (W, H):
            img = img.resize((W, H), Image.LANCZOS)
        return img, None
    if policy == "fit":
        scale = min(W / w, H / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        sheet = Image.new("RGB", (W, H), "white")
        sheet.paste(img.resize((nw, nh), Image.LANCZOS), ((W - nw) // 2, (H - nh) // 2))
        return sheet, f"Image is {w}x{h} (not 4:7), so it was scaled to fit and centered"
    raise SheetError(f"Image is {w}x{h}; expected a 4:7 shape (1200x2100 is ideal)")


def _find_shapes(img, mode, offset, white):
    """Return (shapes, warning, artwork_bbox).

    shapes are point lists in sheet pixels, drawn `offset` px outside the artwork.
    artwork_bbox is (x0, y0, x1, y1) of the artwork itself, ignoring specks.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        mask = img.convert("L").point(lambda v: 255 if v < white else 0)
        box = mask.getbbox()
        if not box:
            raise SheetError("No artwork found on the sheet")
        x0, y0, x1, y1 = box
        pts = [(x0 - offset, y0 - offset), (x1 + offset, y0 - offset),
               (x1 + offset, y1 + offset), (x0 - offset, y1 + offset)]
        return [pts], "OpenCV/numpy not installed: using one rectangle around all artwork", box

    arr = np.array(img)
    raw = (arr.min(axis=2) < white).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * offset + 1, 2 * offset + 1))
    mask = cv2.dilate(raw, kernel)
    found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    found = [c for c in found if cv2.contourArea(c) >= MIN_AREA]
    if not found:
        raise SheetError("No artwork found on the sheet (is the image all white?)")
    shapes = []
    for c in found:
        if mode == "rect":
            x, y, bw, bh = cv2.boundingRect(c)
            pts = [(x, y), (x + bw, y), (x + bw, y + bh), (x, y + bh)]
        else:
            pts = [(int(p[0][0]), int(p[0][1])) for p in cv2.approxPolyDP(c, 1.5, True)]
        shapes.append(pts)
    kept = np.zeros(raw.shape, np.uint8)
    cv2.drawContours(kept, found, -1, 255, -1)
    ys, xs = np.where((raw > 0) & (kept > 0))
    return shapes, None, (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _shrink_to_fit(img, bbox, offset):
    """Scale the artwork down about its own center so the cut line (offset px outside it)
    stays inside the blade-safe area. Returns (new image, percent shrunk) or None."""
    ax0, ay0, ax1, ay1 = bbox
    bw, bh = ax1 - ax0, ay1 - ay0
    cx, cy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
    lo_x, hi_x = SIDE + offset, W - SIDE - offset
    lo_y, hi_y = TOP + offset, H - TOP - offset
    s = min(1.0, 2 * (cx - lo_x) / bw, 2 * (hi_x - cx) / bw,
            2 * (cy - lo_y) / bh, 2 * (hi_y - cy) / bh)
    if s >= 0.995:
        return None
    if s < 0.5:
        raise SheetError("Artwork is too close to the sheet edge to fit safely "
                         "(it would need to shrink by more than half)")
    nw, nh = max(1, round(bw * s)), max(1, round(bh * s))
    crop = img.crop((ax0, ay0, ax1, ay1)).resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGB", (W, H), "white")
    out.paste(crop, (round(cx - nw / 2), round(cy - nh / 2)))
    return out, round((1 - s) * 100)


def prepare_sheet(src, out_jpg, out_plt, out_thumb, cfg):
    """Build the sheet, cut file and a preview thumbnail. Returns a result dict."""
    warnings = []
    try:
        img = _load_flat(src)
    except Exception as e:
        raise SheetError(f"Could not read the image ({e})")
    img, note = _to_sheet(img, cfg.get("non_4x7", "reject"))
    if note:
        warnings.append(note)

    offset = int(cfg.get("cut_offset_px", 20))
    mode, policy = cfg.get("cut_mode", "contour"), cfg.get("edge_policy", "shrink")
    shapes, note, bbox = _find_shapes(img, mode, offset, 235)
    if note:
        warnings.append(note)
    if policy == "shrink":
        shrunk = _shrink_to_fit(img, bbox, offset)
        if shrunk:
            img, pct = shrunk
            shapes, _, _ = _find_shapes(img, mode, offset, 235)
            warnings.append(f"Artwork was scaled down {pct}% so the cut line stays inside the safe area")
    shapes.sort(key=lambda s: min(p[1] for p in s))

    clamped = False
    for i, pts in enumerate(shapes):
        out = [(min(max(x, SIDE), W - SIDE), min(max(y, TOP), H - TOP)) for x, y in pts]
        if out != pts:
            if policy == "fail":
                raise SheetError(f"Artwork reaches the unsafe sheet edge (sticker {i + 1}); "
                                 f"keep it at least {SIDE}px from the sides and {TOP}px from top/bottom")
            shapes[i] = out
            clamped = True
    if clamped:
        warnings.append("Artwork is close to the sheet edge; the cut line was pulled inward")

    cmds = []
    for pts in shapes:
        p = [to_plt(x, y) for x, y in pts + [pts[0]]]
        cmds.append(f"U{p[0][0]},{p[0][1]}")
        cmds += [f"D{a},{b}" for a, b in p]
    cmds.append("U0,0 @")
    plt_text = "IN VER0.1.0 KP42 " + " ".join(cmds)

    img.save(out_jpg, quality=95)
    with open(out_plt, "w") as f:
        f.write(plt_text)

    thumb = img.copy()
    d = ImageDraw.Draw(thumb)
    for pts in shapes:
        d.line(pts + [pts[0]], fill=(255, 0, 0), width=6)
    thumb.thumbnail((360, 630))
    thumb.save(out_thumb, "JPEG", quality=80)

    return {"stickers": len(shapes), "warnings": warnings}
