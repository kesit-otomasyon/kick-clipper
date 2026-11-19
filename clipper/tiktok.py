"""TikTok: sign-in (once), token refresh, and posting a clip with the Content Posting API.

Keys: credentials/tiktok_app.json {"client_key", "client_secret"} (GitHub: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET).
Token: the access token lives 2 hours, the refresh token 180 days; every refresh returns a new refresh token.
  On the PC it is credentials/tiktok_token.json.
  On GitHub it is kept encrypted in the repository variable TIKTOK_TOKEN_ENC (key: secret TIKTOK_TOKEN_KEY),
  read and written back through the API with secret GH_VARS_PAT, so every job uses the newest refresh token.

Until the app passes TikTok's audit, a public account can only get drafts in its TikTok inbox.

  python -m clipper.tiktok seal              -> encrypts the local token for TIKTOK_TOKEN_ENC (prints it)

  python -m clipper.tiktok url               -> sign-in link (open it, allow, copy the address you land on)
  python -m clipper.tiktok code <address>    -> saves the token
  python -m clipper.tiktok whoami            -> account name (checks the token)
  python -m clipper.tiktok post <file.mp4> "caption"
"""
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "credentials" / "tiktok_app.json"
TOKEN = ROOT / "credentials" / "tiktok_token.json"
REDIRECT = "https://kesit-otomasyon.github.io/kick-clipper/callback.html"
SCOPES = "user.info.basic,video.upload,video.publish"
API = "https://open.tiktokapis.com/v2"


def _app() -> dict:
    if os.environ.get("TIKTOK_CLIENT_KEY"):
        return {"client_key": os.environ["TIKTOK_CLIENT_KEY"], "client_secret": os.environ["TIKTOK_CLIENT_SECRET"]}
    return json.loads(APP.read_text(encoding="utf-8"))


REPO = "kesit-otomasyon/kick-clipper"
VAR = "TIKTOK_TOKEN_ENC"


def _remote() -> bool:
    return bool(os.environ.get("GH_VARS_PAT") and os.environ.get("TIKTOK_TOKEN_KEY"))


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(os.environ["TIKTOK_TOKEN_KEY"].encode())


def _gh(method: str, path: str, **kw):
    r = requests.request(method, f"https://api.github.com/repos/{REPO}/actions/variables{path}", timeout=30,
                         headers={"Authorization": f"Bearer {os.environ['GH_VARS_PAT']}",
                                  "Accept": "application/vnd.github+json"}, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub değişkeni {method} {path}: HTTP {r.status_code}")
    return r


def _load() -> dict:
    if _remote():
        return json.loads(_fernet().decrypt(_gh("GET", f"/{VAR}").json()["value"].encode()))
    return json.loads(TOKEN.read_text(encoding="utf-8"))


def _save(tok: dict):
    tok["saved_at"] = int(time.time())
    _save_raw(tok)


def _save_raw(tok: dict):
    if _remote():
        _gh("PATCH", f"/{VAR}", json={"name": VAR, "value": _fernet().encrypt(json.dumps(tok).encode()).decode()})
        return
    TOKEN.parent.mkdir(exist_ok=True)
    TOKEN.write_text(json.dumps(tok), encoding="utf-8")


def _oauth(data: dict) -> dict:
    a = _app()
    r = requests.post(f"{API}/oauth/token/", data={"client_key": a["client_key"], "client_secret": a["client_secret"], **data},
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError(f"TikTok token hatası: {d.get('error')} {d.get('error_description')}")
    return d


def auth_url() -> str:
    q = {"client_key": _app()["client_key"], "scope": SCOPES, "response_type": "code",
         "redirect_uri": REDIRECT, "state": secrets.token_urlsafe(12)}
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(q)


def exchange(landed: str):
    """landed: the callback address (or just the code)."""
    code = urllib.parse.parse_qs(urllib.parse.urlparse(landed).query).get("code", [landed])[0]
    _save(_oauth({"code": code, "grant_type": "authorization_code", "redirect_uri": REDIRECT}))


def access_token() -> str:
    """The newest token is read every time (another job may have refreshed it); refreshed when near expiry."""
    tok = _load()
    if time.time() > tok["saved_at"] + tok["expires_in"] - 300:
        try:
            new = _oauth({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        except RuntimeError:
            time.sleep(5)  # another job refreshed at the same moment: its token is the one to use
            tok = _load()
            if time.time() > tok["saved_at"] + tok["expires_in"] - 300:
                raise
            return tok["access_token"]
        new["sent"] = tok.get("sent", {})  # the daily send count survives the refresh
        _save(new)
        tok = new
    return tok["access_token"]


def _call(path: str, body: dict | None = None, params: dict | None = None) -> dict:
    r = requests.post(f"{API}{path}", params=params, json=body or {}, timeout=60,
                      headers={"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json; charset=UTF-8"})
    d = r.json()
    err = d.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok {path}: {err.get('code')} {err.get('message')}")
    return d.get("data", {})


def whoami() -> str:
    r = requests.get(f"{API}/user/info/", params={"fields": "open_id,display_name"}, timeout=30,
                     headers={"Authorization": f"Bearer {access_token()}"})
    u = r.json().get("data", {}).get("user", {})
    return str(u.get("display_name"))


def post(video: Path, caption: str, public: bool = False, draft: bool = True) -> str:
    """Sends the clip to TikTok; returns the publish id.
    draft=True: it lands in the account's TikTok inbox, the owner adds the caption and posts it with one tap
    (the only way before the audit while the account is public). draft=False: posted straight to the profile."""
    size = video.stat().st_size
    chunk = size if size <= 64 * 2**20 else 10 * 2**20
    count = max(1, size // chunk)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    if draft:
        d = _call("/post/publish/inbox/video/init/", {"source_info": source})
    else:
        info = _call("/post/publish/creator_info/query/")
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        privacy = "PUBLIC_TO_EVERYONE" if public and "PUBLIC_TO_EVERYONE" in options else "SELF_ONLY"
        d = _call("/post/publish/video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": privacy, "disable_duet": False,
                          "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000},
            "source_info": source})
    with open(video, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1  # the last chunk takes the rest
            f.seek(start)
            part = f.read(end - start + 1)
            r = requests.put(d["upload_url"], data=part, timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(part)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok yükleme parçası {i + 1}/{count}: HTTP {r.status_code} {r.text[:200]}")
    return d["publish_id"]


def ready() -> bool:
    """Are the TikTok keys and token here (PC file or GitHub secrets)?"""
    keys = bool(os.environ.get("TIKTOK_CLIENT_KEY")) or APP.exists()
    return keys and (_remote() or TOKEN.exists())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def sent_today() -> int:
    """TikTok sends today (UTC), kept next to the token so every job sees the same count."""
    try:
        return int(_load().get("sent", {}).get(_today(), 0))
    except Exception:
        return 0


def _count_send():
    tok = _load()
    tok["sent"] = {_today(): int(tok.get("sent", {}).get(_today(), 0)) + 1}  # only today's count is kept
    _save_raw(tok)  # saved_at stays: it is the token's age, used for its expiry


def send(cfg: dict, video: Path, caption: str, log, score: float | None = None, floor_key: str = "",
         manual: bool = False) -> None:
    """After a YouTube upload: the same clip to TikTok (a draft until the app is audited). Never breaks the run.
    TikTok allows an unaudited app only a few uploads a day, so automatic sends are kept for the best clips:
    at most tiktok.per_day a day, and only when score >= tiktok[floor_key]. Manual requests (a Short asked for
    by hand) skip both checks, so they can always use the room that is left."""
    t = cfg.get("tiktok", {})
    if not t.get("enabled") or not ready():
        return
    if not manual:
        floor = t.get(floor_key) if floor_key else None
        if floor is not None and score is not None and score < floor:
            log(f"TikTok'a gönderilmedi: puan {score} < {floor} (günlük az hak, sadece en iyiler)")
            return
        n = sent_today()
        if n >= t.get("per_day", 99):
            log(f"TikTok'a gönderilmedi: bugünkü otomatik hak doldu ({n}/{t.get('per_day')})")
            return
    try:
        pid = post(video, caption, public=t.get("public", False), draft=t.get("draft", True))
        log(f"TikTok'a gönderildi ({'taslak' if t.get('draft', True) else 'paylaşım'}): {pid}")
    except Exception as e:
        log(f"TikTok'a gönderilemedi: {type(e).__name__}: {e}")
        return
    try:
        _count_send()
    except Exception as e:
        log(f"TikTok sayacı yazılamadı: {type(e).__name__}: {e}")


def status(publish_id: str) -> str:
    return _call("/post/publish/status/fetch/", {"publish_id": publish_id}).get("status", "?")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "url":
        print(auth_url())
    elif cmd == "code":
        exchange(sys.argv[2])
        print("token kaydedildi:", whoami())
    elif cmd == "seal":  # TIKTOK_TOKEN_KEY must be set in the environment
        from cryptography.fernet import Fernet
        print(Fernet(os.environ["TIKTOK_TOKEN_KEY"].encode()).encrypt(TOKEN.read_bytes()).decode())
    elif cmd == "refresh":  # forces a refresh and writes the new token back (checks the whole chain)
        _save(_oauth({"grant_type": "refresh_token", "refresh_token": _load()["refresh_token"]}))
        print("yenilendi ve kaydedildi:", whoami())
    elif cmd == "whoami":
        print(whoami())
    elif cmd == "post":
        pid = post(Path(sys.argv[2]), sys.argv[3])
        for _ in range(30):
            s = status(pid)
            print(s)
            if s in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"):
                break
            time.sleep(10)
    else:
        print(__doc__)
