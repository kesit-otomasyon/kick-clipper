"""Live chat meter: polls a Kick channel's public chat history (no login) and turns it into a "hype" score,
so a sudden flood of laughing / shocked messages marks a moment worth clipping.

Each reply holds only the newest ~25 messages. In a busy chat that cap is hit on every poll, so the
message rate is measured from the time span those 25 messages cover, not from how many were new."""
import json
import re
import threading
import time
from collections import deque

import requests

from .kick import H

ENDPOINTS = ["https://web.kick.com/api/v1/chat/{cid}/history",
             "https://kick.com/api/v2/channels/{cid}/messages"]
LAUGH_EMOTES = re.compile(r"\[emote:\d+:[^\]]*(KEKW|LUL|OMEGALUL|LMAO|ICANT|pepeLaugh|kekw|xd|ahah)[^\]]*\]", re.I)
WOW_EMOTES = re.compile(r"\[emote:\d+:[^\]]*(NOWAY|POG|Pog|monkaS|WAYTOODANK|Wut|D:|Sadge|PepeHands|omg|wtf)[^\]]*\]")
LAUGH_TEXT = re.compile(r"(?:a?h[ae]){3,}|(?:sj){2,}|(?:jsj)|(?:dsj)|\bxd+\b|\blo+l\b|\bkek\b|g[uü]ld[uü]m|"
                        r"[oö]ld[uü]m|kopt[uu]m|aaa+|ahsj|asdf|djs", re.I)
WOW_TEXT = re.compile(r"ne olu|n[ae]p[ıi]yo|\bwtf\b|\bomg\b|yok art[ıi]k|vay|haha|\?{2,}|!{2,}|efsane|i[sş]te bu|"
                      r"rezil|\bga+l\b|go+l\b", re.I)
CLIP_TEXT = re.compile(r"\bkesit|\bclip\b|\bklip\b|kesitlik|klipl", re.I)


def _kind(text: str) -> str:
    """'' (ordinary), 'hype' (laugh / shock / keysmash / shouting) or 'clip' (asks for a clip)."""
    t = text.strip()
    if CLIP_TEXT.search(t):
        return "clip"
    if LAUGH_EMOTES.search(t) or WOW_EMOTES.search(t) or LAUGH_TEXT.search(t) or WOW_TEXT.search(t):
        return "hype"
    plain = re.sub(r"\[emote:[^\]]*\]", "", t)
    letters = [c for c in plain if c.isalpha()]
    if len(letters) >= 6 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return "hype"  # SHOUTING
    if len(letters) >= 8 and len(set(plain.lower())) <= len(plain) / 3.5:
        return "hype"  # keysmash
    return ""


def _when(m: dict) -> float:
    """Message time (epoch s): the millisecond ref when it agrees with the server time, else the server time."""
    from datetime import datetime
    server = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp()
    try:
        ref = int(json.loads(m.get("metadata") or "{}").get("message_ref", 0)) / 1000
        if abs(ref - server) < 3:
            return ref
    except (ValueError, TypeError):
        pass
    return server + 0.5


class ChatMeter:
    """Background poller. `window(t0, t1)` -> stats of the chat between two wall-clock times."""

    def __init__(self, channel_id: int, every: float = 1.5):
        self.cid, self.every = channel_id, every
        self.msgs = deque(maxlen=20000)   # (time, user, kind)
        self.polls = deque(maxlen=4000)   # (wall, rate msgs/s or None, capped)
        self.seen = deque(maxlen=3000)
        self._seen = set()
        self.errors, self.ok = 0, 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        k, wait = 0, self.every
        while not self._stop.is_set():
            url = ENDPOINTS[k % len(ENDPOINTS)].format(cid=self.cid)
            k += 1
            try:
                r = requests.get(url, headers=H, timeout=15)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                self._take(r.json()["data"]["messages"])
                self.ok += 1
                wait = self.every
            except Exception:  # never let chat kill the watcher: back off and keep going
                self.errors += 1
                wait = min(wait * 2, 60)
            self._stop.wait(wait)

    def _take(self, ms: list[dict]):
        new = 0
        times = []
        for m in ms:
            if m.get("type") != "message":
                continue
            text = m.get("content") or ""
            if text.startswith("!") or re.fullmatch(r"\s*https?://\S+\s*", text):
                continue  # bot commands, bare links
            t = _when(m)
            times.append(t)
            if m["id"] in self._seen:
                continue
            new += 1
            self._seen.add(m["id"])
            self.seen.append(m["id"])
            if len(self._seen) > len(self.seen):
                self._seen = set(self.seen)
            user = (m.get("sender") or {}).get("id") or m.get("user_id")
            self.msgs.append((t, user, _kind(text)))
        capped = len(ms) >= 20 and new >= len(ms) - 1
        rate = None
        if len(times) >= 8:
            span = max(times) - min(times)
            rate = (len(times) - 1) / max(span, 0.5)
        self.polls.append((time.time(), rate, capped))

    def window(self, t0: float, t1: float) -> dict:
        """Chatters, hype share and message rate between t0 and t1 (message times)."""
        ms = [m for m in list(self.msgs) if t0 <= m[0] < t1]
        users = {m[1] for m in ms}
        hype_users = {m[1] for m in ms if m[2]}
        clip_users = {m[1] for m in ms if m[2] == "clip"}
        dur = max(t1 - t0, 1)
        counted = len(ms) / dur
        capped_rates = [r for w, r, c in list(self.polls) if c and r and t0 <= w - 3 < t1]
        rate = max([counted] + capped_rates) if capped_rates else counted
        hype = len(hype_users) / len(users) if users else 0.0
        return {"msgs": len(ms), "users": len(users), "hype_users": len(hype_users), "clip_users": len(clip_users),
                "rate": rate, "hype": hype, "score": rate * (0.3 + hype)}

    def burst(self, now: float, c: dict) -> dict | None:
        """Is the chat exploding right now? Compares the last `win` seconds with the last 5 minutes.
        Returns the window stats plus `why` when it is, else None. `now` is the current wall time."""
        win = c.get("chat_window_s", 10)
        t1 = now - 2                       # the newest second is still arriving
        cur = self.window(t1 - win, t1)
        past = [self.window(t1 - k * win, t1 - (k - 1) * win)["score"] for k in range(2, 300 // win + 1)]
        past = [s for s in past if s > 0]
        if len(past) < 10:                 # not enough history yet
            return None
        base = sorted(past)[len(past) // 2]
        cur["base"] = base
        clips = self.window(now - 22, t1)["clip_users"]
        if clips >= c.get("chat_clip_users", 3):
            cur["why"] = f"{clips} kişi 'kesit' yazdı"
            return cur
        if (cur["score"] >= c.get("chat_ratio", 3.0) * base
                and cur["hype_users"] >= c.get("chat_min_hype_users", 4)
                and cur["hype"] >= c.get("chat_min_hype", 0.35)):
            cur["why"] = f"sohbet x{cur['score'] / base:.1f} ({cur['hype_users']} kişi gülüyor/şaşırıyor)"
            return cur
        return None
