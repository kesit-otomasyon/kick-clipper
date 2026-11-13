"""Watch one live Kick stream and turn its loudest moments (shouting, laughing) into Shorts, while the
stream is still on. Runs on a GitHub runner: the stream is recorded there in 5-second pieces (only the
last few minutes are kept), nothing is downloaded to the user's PC.

python -m clipper.watch <streamer> [--hours 5.6] [--dry-run]"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from . import kick, render, youtube
from .llm import LLM

ROOT = Path(__file__).resolve().parent.parent
SEG = 5  # seconds per recorded piece


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


class Recorder:
    """ffmpeg copies the live stream into numbered 5 s pieces; restarted when the stream token expires."""

    def __init__(self, master: str, workdir: Path):
        self.master, self.dir = master, workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proc, self.next_no, self.pieces = None, 0, []  # pieces: (path, wall start)

    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        url = kick.best_variant(self.master)
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-i", url, "-c", "copy", "-f", "segment", "-segment_time", str(SEG),
             "-reset_timestamps", "1", "-segment_start_number", str(self.next_no), str(self.dir / "p%07d.ts")])
        log(f"kayıt başladı ({url.split('/')[-2] if '/' in url else 'stream'})")

    def finished(self) -> list[Path]:
        """New complete pieces (every piece except the one being written)."""
        files = sorted(self.dir.glob("p*.ts"))
        known = {p for p, _ in self.pieces}
        new = [f for f in files[:-1] if f not in known]
        for f in new:
            self.pieces.append((f, time.time() - SEG))
            self.next_no = max(self.next_no, int(f.stem[1:]) + 1)
        for f, _ in self.pieces[:-80]:  # keep ~6-7 minutes
            f.unlink(missing_ok=True)
        self.pieces = self.pieces[-80:]
        return new

    def cut(self, wall_from: float, wall_to: float, out: Path) -> Path | None:
        use = [p for p, t in self.pieces if t + SEG > wall_from and t < wall_to and p.exists()]
        if len(use) < 3:
            return None
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in use), encoding="utf-8")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(out)], check=True)
        return out

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def loudness(piece: Path) -> list[float]:
    """dB per second of a piece (8 kHz mono)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(piece), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le",
                          "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    n = len(a) // 8000
    if n == 0:
        return []
    return list(20 * np.log10(np.sqrt((a[:n * 8000].reshape(n, 8000) ** 2).mean(1)) + 1))


def describe(llm: LLM, words, streamer: str, stream_title: str, category: str) -> dict:
    said = " ".join(w[2] for w in words)[:1500] or "(only shouting / laughing, no clear words)"
    return llm.json(f"""A moment from a live Kick stream by the Turkish streamer {streamer}
(stream title: "{stream_title}", category: {category}). What was said:
{said}

1) Is it safe for YouTube? ok=false for: slurs or hate, sexual content, doxxing or private data, self-harm,
   gambling/casino, violence against real people. Normal swearing and game violence are fine.
2) A Turkish hook shown on top for the first seconds: max 5 words, makes people stay, no swearing, no emojis.
3) A Turkish YouTube title: max 55 chars, mentions {streamer}, ends with " #shorts", no swearing.
4) 6-10 Turkish/English tags.
Return JSON {{"ok": true|false, "reason": "...", "hook": "...", "title": "...", "tags": ["..."]}}""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("streamer")
    ap.add_argument("--hours", type=float, default=5.6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    c = cfg["clips"]
    who = args.streamer
    info = kick.live_info(who)
    if not info:
        log(f"{who} yayında değil.")
        return
    if info["category"] in cfg.get("blocked_categories", []):
        log(f"{who} kategorisi engelli ({info['category']}), izlenmiyor.")
        return
    started = datetime.fromisoformat(info["started"].replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp()
    log(f"{who} yayında: {info['title'][:60]} | {info['category']}")

    channel = cfg["youtube_channel"]
    uploads = [] if args.dry_run else youtube.recent_uploads(channel)
    done = youtube.moments_done(uploads)
    mine_today = sum(f"kick.com/{who}" in u["description"] for u in uploads[:youtube.uploads_today(uploads)])
    made = 0
    llm = LLM(cfg)
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    rec = Recorder(info["playback_url"], ROOT / "work" / who)
    db, walls = [], []          # dB per second and its wall-clock time
    last_clip, pending = 0.0, None
    deadline = time.time() + args.hours * 3600
    next_check, offline = time.time() + 120, 0

    try:
        while time.time() < deadline:
            rec.ensure()
            time.sleep(2)
            for piece in rec.finished():
                t0 = time.time() - SEG
                for k, v in enumerate(loudness(piece)):
                    db.append(v)
                    walls.append(t0 + k)
            db, walls = db[-900:], walls[-900:]

            if time.time() > next_check:  # still live? same stream?
                next_check = time.time() + 120
                if len(db) > 10:  # heartbeat: how much was recorded and how loud it got
                    w = db[-120:]
                    base = float(np.median(w))
                    top = max(float(np.mean(w[i - 4:i])) - base for i in range(4, len(w) + 1))
                    log(f"durum: {len(rec.pieces)} parça, {len(db)} sn ses, son 2 dk en büyük sıçrama +{top:.1f} dB")
                else:
                    log(f"durum: henüz ses yok ({len(rec.pieces)} parça)")
                now = kick.live_info(who)
                offline = offline + 1 if not now or now["id"] != info["id"] else 0
                if offline >= 2:
                    log("yayın bitti.")
                    break

            # a burst: the last 4 s are much louder than the last 2 minutes
            if pending is None and len(db) > 120:
                base = float(np.median(db[-120:]))
                burst = float(np.mean(db[-4:])) - base
                peak_wall = walls[-2]
                in_stream = peak_wall - started
                if (burst >= c["burst_db"] and peak_wall - last_clip > c["cooldown_s"]
                        and in_stream > c["min_seconds_live"]):
                    pending = (peak_wall, burst)
                    log(f"patlama: +{burst:.1f} dB (yayının {in_stream / 60:.0f}. dakikası)")

            if pending and time.time() > pending[0] + c["after_s"] + SEG:
                peak, burst = pending
                pending, last_clip = None, peak
                sec = int(peak - started)
                if any(sid == info["id"] and abs(s - sec) < 60 for sid, s in done):
                    log("bu an zaten yüklenmiş, atlandı.")
                    continue
                if not args.dry_run and (mine_today + made >= c["per_streamer_per_day"]
                                         or youtube.uploads_today(youtube.recent_uploads(channel)) >= c["per_day_total"]):
                    log("günlük kesit sınırı doldu, sadece izleniyor.")
                    continue
                stamp = f"{who}_{info['id']}_{sec}"
                raw = rec.cut(peak - c["before_s"], peak + c["after_s"], out / f"{stamp}_raw.mp4")
                if not raw:
                    continue
                words = render.transcribe(raw)
                try:
                    meta = describe(llm, words, who, info["title"], info["category"])
                except RuntimeError as e:
                    log(f"başlık yazılamadı: {e}")
                    continue
                if not meta.get("ok", False):
                    log(f"güvenlik kontrolü reddetti: {meta.get('reason', '')}")
                    raw.unlink(missing_ok=True)
                    continue
                final = render.render(raw, out / f"{stamp}.mp4", words, meta.get("hook", ""), who)
                raw.unlink(missing_ok=True)
                title = meta.get("title") or f"{who} yayınından #shorts"
                desc = (f"{who} canlı yayınından bir an ({info['category']}).\n"
                        f"Yayıncı: https://kick.com/{who}\n\n#{who} #kick #shorts\n\nmoment:{info['id']}:{sec}")
                (out / f"{stamp}.json").write_text(json.dumps({"title": title, "desc": desc, "burst_db": round(burst, 1),
                                                               **meta}, ensure_ascii=False, indent=1), encoding="utf-8")
                if args.dry_run:
                    log(f"[deneme] hazır: {final.name} | {title}")
                else:
                    vid = youtube.upload(channel, final, title, desc, meta.get("tags", []), c["publish_delay_min"])
                    log(f"yüklendi: https://youtu.be/{vid} | {title}")
                    final.unlink(missing_ok=True)
                made += 1
                done.add((info["id"], sec))
    finally:
        rec.stop()
    # tell the workflow whether to start the next shift
    still = kick.live_info(who)
    (ROOT / "still_live").write_text("1" if still and still["id"] == info["id"] else "0")


if __name__ == "__main__":
    sys.exit(main())
