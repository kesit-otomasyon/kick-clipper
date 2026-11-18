"""Watch one live Kick stream and turn its big moments into Shorts while the stream is still on.
A moment is found two ways: the live chat suddenly floods with laughing / shocked messages or people
asking for a clip (main signal), or the stream audio jumps (shouting, laughing). Runs on a GitHub runner: the
stream is recorded there in 5-second pieces (only the last few minutes are kept), nothing is downloaded to the
user's PC.

Detection never waits: each moment's pieces are copied aside and a worker thread prepares it (picture check,
transcript, title, render). Only a few Shorts per streamer fit in a day, so instead of the first moments, the
strongest prepared moment of every `pick_every_min` window is uploaded (and the best one left when the stream ends).

python -m clipper.watch <streamer> [--hours 5.6] [--dry-run]"""
import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from . import cam, kick, render, youtube
from .chat import ChatMeter
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

    def grab(self, wall_from: float, wall_to: float, folder: Path) -> list[Path]:
        """Copies the pieces of a moment aside (instant), so they survive the rolling delete."""
        use = [p for p, t in self.pieces if t + SEG > wall_from and t < wall_to and p.exists()]
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for p in use:
            try:
                out.append(Path(shutil.copy2(p, folder / p.name)))
            except OSError:
                pass
        return out

    def cut(self, wall_from: float, wall_to: float, out: Path) -> Path | None:
        return join(self.grab(wall_from, wall_to, out.parent / (out.stem + "_parts")), out)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def join(pieces: list[Path], out: Path) -> Path | None:
    """Pieces -> one clip (re-encoded so the cuts between pieces are clean)."""
    if len(pieces) < 3:
        return None
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in pieces), encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(out)], check=True)
    lst.unlink(missing_ok=True)
    return out


def loudness(piece: Path) -> list[float]:
    """dB per second of a piece (8 kHz mono)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(piece), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le",
                          "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    n = len(a) // 8000
    if n == 0:
        return []
    return list(20 * np.log10(np.sqrt((a[:n * 8000].reshape(n, 8000) ** 2).mean(1)) + 1))


def describe(llm: LLM, words, streamer: str, stream_title: str, category: str, frames=None) -> dict:
    said = " ".join(w[2] for w in words)[:1500] or "(only shouting / laughing, no clear words)"
    seen = f"""
The {len(frames)} images are frames from the start, middle and end of the clip: judge what is ON SCREEN.
visual = 1-10, how watchable the picture is as a Short. Give 1-3 when the screen is mostly a loading, setup,
settings or OBS window, a desktop or browser, a "no signal" / black / "starting soon" / "be right back" /
ending screen, a paused game or menu, a sponsor ad or promo code, a still image, or the streamer is away.
Give 7-10 when something is clearly happening (gameplay action, a strong reaction on the webcam, a funny scene).
scene = what you see, one short English sentence.
""" if frames else ""
    return llm.json(f"""A moment from a live Kick stream by the Turkish streamer {streamer}
(stream title: "{stream_title}", category: {category}). What was said:
{said}
{seen}
1) Is it safe for YouTube? ok=false for: slurs or hate, sexual content, doxxing or private data, self-harm,
   gambling/casino, violence against real people. Normal swearing and game violence are fine.
   Never set ok=false because a game looks new, unreleased or fake: judge only what is said and shown.
2) A Turkish hook shown on top for the first seconds: max 5 words, makes people stay, no swearing, no emojis.
   It must fit what is on screen.
3) A Turkish YouTube title: max 55 chars, mentions {streamer}, ends with " #shorts", no swearing.
4) 6-10 Turkish/English tags.
Return JSON {{"ok": true|false, "reason": "...", "visual": 1-10, "scene": "...", "hook": "...", "title": "...",
"tags": ["..."]}}""", images=frames)


def picture_ok(raw, c: dict) -> tuple[dict | None, str]:
    """Cheap check of the picture before transcribing: (look result, '') or (None, why it was dropped)."""
    seen = render.look(raw)
    if seen["black"] > c.get("max_black", 0.3):
        return None, f"ekranın %{seen['black'] * 100:.0f}'i siyah"
    if seen["frozen"] > c.get("max_frozen", 0.7):
        return None, f"görüntünün %{seen['frozen'] * 100:.0f}'i donuk (hareket yok)"
    return seen, ""


def visual_ok(meta: dict, c: dict) -> str:
    """'' when the vision check liked the picture, else why not."""
    try:
        v = float(meta.get("visual", 5))
    except (TypeError, ValueError):
        v = 5.0
    if v < c.get("min_visual", 5):
        return f"görüntü zayıf ({v:.0f}/10): {meta.get('scene', '')}"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("streamer")
    ap.add_argument("--hours", type=float, default=5.6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    c = cfg["clips"]
    who = args.streamer
    if who not in cfg["streamers"] and not args.dry_run:
        log(f"{who} izin verilen yayıncılar listesinde değil; sadece --dry-run ile denenebilir.")
        return
    info = kick.live_info(who)
    if not info:
        log(f"{who} yayında değil.")
        return
    if info["category"] in cfg.get("blocked_categories", []):
        log(f"{who} kategorisi engelli ({info['category']}), izlenmiyor.")
        return
    started = datetime.fromisoformat(info["started"].replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp()
    log(f"{who} yayında: {info['title'][:60]} | {info['category']}")

    w = Worker(cfg, who, info, started, args.dry_run)
    rec = Recorder(info["playback_url"], ROOT / "work" / who)
    meter = ChatMeter(info["channel_id"]).start()
    db, walls = [], []          # dB per second and its wall-clock time
    last_clip, pending = 0.0, None
    deadline = time.time() + args.hours * 3600
    next_check, offline = time.time() + 120, 0
    next_cam = time.time() + 90  # first look soon after the start, then every cam_every_min

    try:
        while time.time() < deadline and not w.full():
            rec.ensure()
            time.sleep(2)
            for piece in rec.finished():
                t0 = time.time() - SEG
                for k, v in enumerate(loudness(piece)):
                    db.append(v)
                    walls.append(t0 + k)
            db, walls = db[-900:], walls[-900:]

            if time.time() > next_cam and len(rec.pieces) >= 13:
                next_cam = time.time() + c.get("cam_every_min", 30) * 60
                ps = [rec.pieces[-1][0], rec.pieces[-7][0], rec.pieces[-13][0]]  # 30 s apart
                threading.Thread(target=w.check_cam, args=(ps,), daemon=True).start()

            if time.time() > next_check:  # still live? same stream?
                next_check = time.time() + 120
                if len(db) > 10:  # heartbeat: how much was recorded and how loud it got
                    hb = db[-120:]
                    base = float(np.median(hb))
                    top = max(float(np.mean(hb[i - 4:i])) - base for i in range(4, len(hb) + 1))
                    log(f"durum: {len(rec.pieces)} parça, {len(db)} sn ses, son 2 dk en büyük sıçrama +{top:.1f} dB, "
                        f"hazır aday {len(w.ready)}, sırada {w.q.qsize()}")
                else:
                    log(f"durum: henüz ses yok ({len(rec.pieces)} parça)")
                cw = meter.window(time.time() - 62, time.time() - 2)
                log(f"sohbet: {cw['rate']:.1f} mesaj/sn, {cw['users']} kişi, %{cw['hype'] * 100:.0f} gülme/şaşırma, "
                    f"istek {meter.ok} ok / {meter.errors} hata")
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
                    pending = (peak_wall, f"ses +{burst:.1f} dB", burst / c["burst_db"])
                    log(f"patlama (ses): +{burst:.1f} dB (yayının {in_stream / 60:.0f}. dakikası)")

            # a burst in chat: viewers react a few seconds after the moment, so the peak is moved back
            if pending is None:
                now = time.time()
                b = meter.burst(now, c)
                peak_wall = now - c.get("chat_delay_s", 12)
                if (b and peak_wall - last_clip > c["cooldown_s"]
                        and peak_wall - started > c["min_seconds_live"]):
                    strength = max(b["score"] / max(b["base"], 1e-6) / c.get("chat_ratio", 3.0), 1.0)
                    if "kesit" in b["why"]:
                        strength += 0.5
                    pending = (peak_wall, b["why"], strength * 1.2)  # chat is the stronger signal
                    log(f"patlama (sohbet): {b['why']} | skor {b['score']:.1f}, taban {b['base']:.1f} "
                        f"(yayının {(peak_wall - started) / 60:.0f}. dakikası)")

            if pending and time.time() > pending[0] + c["after_s"] + SEG:
                peak, why, strength = pending
                pending, last_clip = None, peak
                sec = int(peak - started)
                parts = rec.grab(peak - c["before_s"], peak + c["after_s"], ROOT / "work" / "moments" / f"{who}_{sec}")
                if len(parts) >= 3:
                    w.q.put({"sec": sec, "why": why, "strength": strength, "parts": parts})
            w.tick()
    finally:
        rec.stop()
        meter.stop()
        try:
            w.finish()
        finally:
            # tell the workflow whether to start the next shift (also after a crash)
            try:
                still = kick.live_info(who)
                live = bool(still and still["id"] == info["id"])
            except Exception:
                live = True  # unsure: better to start another shift, it stops by itself when offline
            if w.left() <= 0:
                live = False  # nothing more can be uploaded today: no next shift (it would only loop)
            (ROOT / "still_live").write_text("1" if live else "0")


class Worker:
    """Prepares moments in the background and decides which ones are uploaded."""

    def __init__(self, cfg: dict, who: str, info: dict, started: float, dry: bool):
        self.cfg, self.c, self.who, self.info, self.started, self.dry = cfg, cfg["clips"], who, info, started, dry
        self.channel = cfg["youtube_channel"]
        self.llm = LLM(cfg)
        self.out = ROOT / "out"
        self.out.mkdir(exist_ok=True)
        uploads = [] if dry else youtube.recent_uploads(self.channel)
        self.done = youtube.moments_done(uploads)
        self.mine_today = sum(f"kick.com/{who}" in u["description"] for u in uploads[:youtube.uploads_today(uploads)])
        self.all_today = youtube.uploads_today(uploads)  # every upload today, live watch and viewer clips
        self.made = 0
        self.cam = cfg.get("cams", {}).get(who)  # webcam box, looked for again every 30 minutes
        self.q: queue.Queue = queue.Queue()
        self.ready: list[dict] = []
        self.lock = threading.Lock()
        self.window_end = time.time() + self.c.get("pick_every_min", 30) * 60
        self.busy = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def check_cam(self, pieces):
        """Where is the webcam now? A layout change is followed; when unsure the last box stays."""
        try:
            frs = cam.frames(pieces)
            box = cam.find(self.llm, frs)
            if box is None and self.cam and cam.people_in(frs, self.cam) >= 2:
                box = self.cam  # e.g. a full-screen cutscene confused the look, but people are still in the old box
        except Exception as e:
            log(f"kamera kontrolü yapılamadı: {type(e).__name__}: {e}")
            return
        if box != self.cam:
            log(f"kamera: {cam.where(box)} {box or ''} (önceki: {cam.where(self.cam)})")
        self.cam = box

    def left(self) -> int:
        if self.dry:
            return 99
        return min(self.c["per_streamer_per_day"] - self.mine_today - self.made,
                   self.c["per_day_total"] - self.all_today - self.made)

    def full(self) -> bool:
        """Nothing more can be uploaded today: stop watching (saves the runner)."""
        if self.left() > 0:
            return False
        log("günlük kesit sınırı doldu, izleme bitiyor.")
        return True

    def _run(self):
        while True:
            m = self.q.get()
            if m is None:
                return
            self.busy.set()
            try:
                self._prepare(m)
            except Exception as e:  # one bad moment must not stop the others
                log(f"aday hazırlanamadı ({m['sec']} sn): {type(e).__name__}: {e}")
            finally:
                shutil.rmtree(m["parts"][0].parent, ignore_errors=True)
                for f in self.out.glob(f"{self.who}_{self.info['id']}_{m['sec']}_raw.*"):
                    f.unlink(missing_ok=True)
                self.busy.clear()

    def _prepare(self, m: dict):
        c, who, info = self.c, self.who, self.info
        sec = m["sec"]
        if any(sid == info["id"] and abs(s - sec) < 60 for sid, s in self.done):
            log("bu an zaten yüklenmiş, atlandı.")
            return
        stamp = f"{who}_{info['id']}_{sec}"
        raw = join(m["parts"], self.out / f"{stamp}_raw.mp4")
        if not raw:
            return
        seen, why_not = picture_ok(raw, c)
        if not seen:
            log(f"görüntü elendi ({sec} sn): {why_not}")
            raw.unlink(missing_ok=True)
            return
        words = render.transcribe(raw)
        try:
            meta = describe(self.llm, words, who, info["title"], info["category"], seen["frames"])
        except RuntimeError as e:
            log(f"başlık yazılamadı: {e}")
            raw.unlink(missing_ok=True)
            return
        if not meta.get("ok", False):
            log(f"güvenlik kontrolü reddetti ({sec} sn): {meta.get('reason', '')}")
            raw.unlink(missing_ok=True)
            return
        weak = visual_ok(meta, c)
        if weak:
            log(f"görüntü elendi ({sec} sn): {weak}")
            raw.unlink(missing_ok=True)
            return
        final = render.render(raw, self.out / f"{stamp}.mp4", words, meta.get("hook", ""), who, self.cam)
        raw.unlink(missing_ok=True)
        try:
            visual = float(meta.get("visual", 5))
        except (TypeError, ValueError):
            visual = 5.0
        quality = round(m["strength"] * visual, 2)
        title = meta.get("title") or f"{who} yayınından #shorts"
        desc = (f"{who} canlı yayınından bir an ({info['category']}).\n"
                f"Yayıncı: https://kick.com/{who}\n\n#{who} #kick #shorts\n\nmoment:{info['id']}:{sec}")
        (self.out / f"{stamp}.json").write_text(json.dumps(
            {"title": title, "desc": desc, "trigger": m["why"], "quality": quality, **meta},
            ensure_ascii=False, indent=1), encoding="utf-8")
        with self.lock:
            self.ready.append({"sec": sec, "file": final, "title": title, "desc": desc,
                               "tags": meta.get("tags", []), "quality": quality})
        log(f"aday hazır ({sec} sn): kalite {quality} = güç {m['strength']:.2f} x görüntü {visual:.0f} | "
            f"{meta.get('scene', '')} | {title}")

    def tick(self):
        """At the end of each window, upload the strongest prepared moment."""
        if time.time() < self.window_end:
            return
        try:
            self._publish_best()
            self.window_end = time.time() + self.c.get("pick_every_min", 30) * 60
        except Exception as e:  # keep the moments and try again in 3 minutes
            log(f"yükleme başarısız, 3 dk sonra tekrar denenecek: {type(e).__name__}: {e}")
            self.window_end = time.time() + 180

    def _drop(self, items):
        with self.lock:
            self.ready = [r for r in self.ready if r not in items]
        if not self.dry:  # the other moments of a window are dropped (kept only in a dry run)
            for r in items:
                r["file"].unlink(missing_ok=True)

    def _publish_best(self):
        """Uploads the strongest prepared moment; on a network error everything stays for the next try."""
        with self.lock:
            if not self.ready:
                return
            best = max(self.ready, key=lambda r: r["quality"])
            rest = [r for r in self.ready if r is not best]
        if self.left() <= 0:
            self._drop([best] + rest)
            return
        if not self.dry:
            uploads = youtube.recent_uploads(self.channel)
            self.done |= youtube.moments_done(uploads)
            if any(sid == self.info["id"] and abs(s - best["sec"]) < 60 for sid, s in self.done):
                log("seçilen an bu arada yüklenmiş (izleyici kesiti), atlandı.")
                self._drop([best])
                return
            self.all_today = youtube.uploads_today(uploads) - self.made
            if self.left() <= 0:
                log("günlük toplam yükleme sınırı doldu.")
                self._drop([best] + rest)
                return
            vid = youtube.upload(self.channel, best["file"], best["title"], best["desc"], best["tags"],
                                 self.c["publish_delay_min"])
            log(f"yüklendi (kalite {best['quality']}, {len(rest)} aday elendi): https://youtu.be/{vid} | {best['title']}")
        self._drop([best] + rest)  # files are deleted only outside a dry run
        if self.dry:
            log(f"[deneme] seçildi (kalite {best['quality']}, {len(rest)} aday elendi): {best['file'].name} | "
                f"{best['title']}")
        self.made += 1
        self.done.add((self.info["id"], best["sec"]))

    def finish(self):
        """Stream over (or shift over): let the queue drain, then upload the best moment left."""
        self.q.put(None)
        self.thread.join(timeout=10 * 60)
        for k in range(3):
            try:
                self._publish_best()
                return
            except Exception as e:
                log(f"son yükleme başarısız ({k + 1}/3): {type(e).__name__}: {e}")
                time.sleep(20)


if __name__ == "__main__":
    sys.exit(main())
