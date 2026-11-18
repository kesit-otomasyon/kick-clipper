"""Finds the streamer's webcam box on screen by itself, so a layout change is followed without editing config.

Gemini looks at one frame and says where the webcam overlay is (real people, not game characters);
the box edges are then snapped to the overlay's border lines (they stay still while the game moves),
and the face finder must see someone inside it."""
from pathlib import Path

import cv2
import numpy as np

from .render import FACE_MODEL


def frames(videos: list[Path], per_video: int = 1) -> list[np.ndarray]:
    """A few frames spread over the given videos (pieces some time apart work best)."""
    out = []
    for v in videos:
        cap = cv2.VideoCapture(str(v))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        for k in range(per_video):
            if n > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int((k + 0.5) / per_video * n))
            ok, fr = cap.read()
            if ok:
                out.append(fr)
        cap.release()
    return out


def _snap(frs, x0, y0, x1, y1):
    """Moves each edge to the strongest still line nearby (the overlay's border)."""
    g = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frs]
    H, W = g[0].shape
    gx = np.mean([np.abs(np.diff(a, axis=1)) for a in g], axis=0)  # still edges add up, moving ones blur out
    gy = np.mean([np.abs(np.diff(a, axis=0)) for a in g], axis=0)
    tol_x, tol_y = int(W * 0.04), int(H * 0.05)

    def best(prof, guess, lo, hi):
        lo, hi = max(lo, 0), min(hi, len(prof))
        if hi - lo < 3:
            return guess
        seg = prof[lo:hi]
        k = int(np.argmax(seg))
        return lo + k + 1 if seg[k] > 2.5 * (np.median(seg) + 1e-3) else guess

    ya, yb = max(y0, 0), min(y1, H)
    xa, xb = max(x0, 0), min(x1, W)
    if x0 > W * 0.015:
        x0 = best(gx[ya:yb].mean(axis=0), x0, x0 - tol_x, x0 + tol_x)
    else:
        x0 = 0
    if x1 < W * 0.985:
        x1 = best(gx[ya:yb].mean(axis=0), x1, x1 - tol_x, x1 + tol_x)
    else:
        x1 = W
    if y0 > H * 0.015:
        y0 = best(gy[:, xa:xb].mean(axis=1), y0, y0 - tol_y, y0 + tol_y)
    else:
        y0 = 0
    if y1 < H * 0.985:
        y1 = best(gy[:, xa:xb].mean(axis=1), y1, y1 - tol_y, y1 + tol_y)
    else:
        y1 = H
    return x0, y0, x1, y1


def people_in(frs, box) -> int:
    """In how many frames the face finder sees someone inside box (fractions); small faces are looked at closer."""
    H, W = frs[0].shape[:2]
    x, y, w, h = int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H)
    seen = 0
    for fr in frs:
        crop = fr[y:y + h, x:x + w]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, None, fx=2, fy=2) if h < H * 0.5 else crop
        det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (crop.shape[1], crop.shape[0]), 0.6)
        _, faces = det.detect(crop)
        seen += faces is not None and len(faces) > 0
    return seen


def find(llm, frs: list[np.ndarray]):
    """[x, y, w, h] fractions of the webcam box, None when there is no webcam, raises when unsure (LLM error)."""
    if not frs:
        raise ValueError("kare yok")
    H, W = frs[0].shape[:2]
    jpg = cv2.imencode(".jpg", cv2.resize(frs[-1], (1280, int(1280 * H / W))), [cv2.IMWRITE_JPEG_QUALITY, 85])[1]
    a = llm.json("""This is a frame of a live game stream. Streamers often put a WEBCAM overlay on top of the game:
a rectangle (usually in a corner) showing the real streamer or a group of real people in a room.
Game characters, cutscenes, trailers and videos on screen are NOT the webcam.
If the whole screen is the camera (just chatting, no game), that is not an overlay: webcam=false.
Answer JSON: {"webcam": true/false, "box_2d": [ymin, xmin, ymax, xmax]} with the overlay's outer border,
coordinates normalized to 0-1000.""", images=[jpg.tobytes()])
    if not a.get("webcam") or len(a.get("box_2d") or []) != 4:
        return None
    y0, x0, y1, x1 = (float(v) / 1000 for v in a["box_2d"])
    x0, y0, x1, y1 = _snap(frs, int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
    w, h = (x1 - x0) / W, (y1 - y0) / H
    if not (0.08 < w < 0.7 and 0.08 < h < 0.7):  # not a corner overlay
        return None
    box = [round(x0 / W, 3), round(y0 / H, 3), round(w, 3), round(h, 3)]
    return box if people_in(frs, box) >= max(1, len(frs) // 3) else None


def where(box) -> str:
    if not box:
        return "yok"
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    return ("üst" if cy < 0.4 else "alt" if cy > 0.6 else "orta") + " " + \
        ("sol" if cx < 0.4 else "sağ" if cx > 0.6 else "orta")
