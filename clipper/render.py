"""A stream moment -> vertical Short: webcam on top when there is one (found with a face detector),
large gameplay otherwise; Turkish word-timed captions (spoken word in yellow), hook, streamer credit."""
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "assets" / "fonts"
FACE_MODEL = ROOT / "models" / "face_yunet.onnx"
SWEARS = re.compile(r"\b(sik\w*|am[ıi]na?\w*|orospu\w*|piç\w*|göt\w*|yarra\w*|yarak\w*|siktir\w*|amk|aq|mk)\b", re.I)
_whisper = None


def tr_upper(t: str) -> str:
    return t.replace("i", "İ").replace("ı", "I").upper()


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def find_facecam(src: Path, dur: float):
    """(x, y, w, h) of the webcam box in source pixels, or None (faces must stay in one place)."""
    cap = cv2.VideoCapture(str(src))
    W, H = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (int(W), int(H)), 0.8)
    hits = []
    for k in range(8):
        cap.set(cv2.CAP_PROP_POS_MSEC, (k + 0.5) / 8 * dur * 1000)
        ok, fr = cap.read()
        if not ok:
            continue
        _, faces = det.detect(fr)
        # a webcam is a small box near an edge; a big face in the middle is a video/game on screen
        # (an eray test picked a GTA artwork face as the "webcam")
        faces = [f[:4] for f in (faces if faces is not None else [])
                 if H * 0.06 <= f[3] <= H * 0.22 and not (W * 0.3 < f[0] + f[2] / 2 < W * 0.7)]
        if faces:
            hits.append(max(faces, key=lambda f: f[2] * f[3]))
    cap.release()
    if len(hits) < 4:
        return None
    a = np.array(hits, float)
    cx, cy, s = np.median(a[:, 0] + a[:, 2] / 2), np.median(a[:, 1] + a[:, 3] / 2), np.median(a[:, 2])
    if sum(abs(h[0] + h[2] / 2 - cx) < s and abs(h[1] + h[3] / 2 - cy) < s for h in a) < 4:
        return None
    w = max(s * 3.2, s * 2.6 * 1080 / 700)
    h = w * 700 / 1080
    x = min(max(cx - w / 2, 0), W - w)
    y = min(max(cy - h / 2 + s * 0.2, 0), H - h)
    return int(x), int(y), int(w), int(h)


def transcribe(video: Path):
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("small", device="cpu", compute_type="int8")
    segs, _ = _whisper.transcribe(str(video), language="tr", word_timestamps=True, vad_filter=True)
    return [(w.start, w.end, w.word.strip()) for s in segs for w in s.words if w.word.strip()]


def _t(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text: str) -> str:
    text = SWEARS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)
    return tr_upper(text.replace("{", "(").replace("}", ")").replace("\\", "/"))


def write_ass(words, hook, credit, dur, path, cap_y, hook_y):
    lines = [f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Anton,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,0,0,0,0,100,100,0,0,3,14,0,8,60,60,{hook_y},1
Style: Cap,Anton,100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,9,3,8,50,50,{cap_y},1
Style: Credit,Anton,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,4,2,2,60,60,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text""",
             f"Dialogue: 0,{_t(0)},{_t(min(dur, 4.5))},Hook,,0,0,0,,{_clean(hook)}",
             f"Dialogue: 0,{_t(0)},{_t(dur)},Credit,,0,0,0,,{_clean(credit)}"]
    chunk = []
    for w in words + [None]:
        if w is not None:
            chunk.append(w)
        if chunk and (w is None or len(chunk) == 3 or w[2].endswith((".", "?", "!"))):
            for k, cw in enumerate(chunk):  # the word being spoken in yellow
                parts = [("{\\c&H00D2FF&}" + _clean(c[2]) + "{\\c&HFFFFFF&}") if j == k else _clean(c[2])
                         for j, c in enumerate(chunk)]
                end = chunk[k + 1][0] if k + 1 < len(chunk) else chunk[-1][1] + 0.15
                lines.append(f"Dialogue: 0,{_t(cw[0])},{_t(end)},Cap,,0,0,0,,{' '.join(parts)}")
            chunk = []
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render(src: Path, out: Path, words, hook: str, streamer: str) -> Path:
    dur = duration(src)
    cam = find_facecam(src, dur)
    ass = out.with_suffix(".ass")
    fonts = out.parent / "fonts"  # relative: Windows drive colons break the ass filter
    if not fonts.exists():
        import shutil
        shutil.copytree(FONTS, fonts)
    if cam:
        x, y, w, h = cam
        vf = (f"[0:v]split[a][b];[a]crop={w}:{h}:{x}:{y},scale=1080:700[cam];"
              "[b]scale=-2:1220,crop=1080:1220[game];[cam][game]vstack[v]")
        write_ass(words, hook, f"KICK: {streamer}", dur, ass, cap_y=1380, hook_y=600)
    else:
        vf = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=30:3,eq=brightness=-0.3[bg];[b]scale=-2:1300,crop=1080:1300[game];[bg][game]overlay=0:300[v]")
        write_ass(words, hook, f"KICK: {streamer}", dur, ass, cap_y=1350, hook_y=150)
    vf += f";[v]ass={ass.name}:fontsdir=fonts[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src.resolve()), "-filter_complex", vf, "-map", "[out]",
                    "-map", "0:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac",
                    "-b:a", "160k", "-r", "30", out.name], check=True, cwd=out.parent)
    return out
