#!/usr/bin/env python3
"""
GTA 6 Stream Clipper Bot
Finds top Twitch clips of GTA 6 (or any game / specific streamers),
converts them to vertical YouTube Shorts, and uploads them.

Run once:      python3 clipper.py
Dry run:       python3 clipper.py --dry-run     (no upload, just builds shorts)
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
HISTORY_FILE = BASE / "posted.json"
DOWNLOADS = BASE / "downloads"
SHORTS = BASE / "shorts"
LOG_FILE = BASE / "clipper.log"
FONTS = BASE / "fonts"


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit("config.json missing — copy config.example.json to config.json and fill it in")
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    # in the cloud, keys come from GitHub Secrets instead of config.json
    for key, env in (("twitch_client_id", "TWITCH_CLIENT_ID"), ("twitch_client_secret", "TWITCH_CLIENT_SECRET")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def load_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"posted_clip_ids": []}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ---------------- Twitch ----------------

def twitch_token(cfg):
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": cfg["twitch_client_id"],
            "client_secret": cfg["twitch_client_secret"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def twitch_get(cfg, token, endpoint, params):
    r = requests.get(
        f"https://api.twitch.tv/helix/{endpoint}",
        headers={"Client-ID": cfg["twitch_client_id"], "Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


def get_game_id(cfg, token, name):
    data = twitch_get(cfg, token, "games", {"name": name})
    if not data:
        sys.exit(f"Game '{name}' not found on Twitch — check the exact category name")
    return data[0]["id"]


def fetch_clips(cfg, token):
    """Top clips from the lookback window, by game and/or specific broadcasters."""
    started_at = (
        datetime.now(timezone.utc) - timedelta(hours=cfg.get("lookback_hours", 6))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = []

    game_id = None
    if cfg.get("game_name"):
        game_id = get_game_id(cfg, token, cfg["game_name"])

    broadcasters = cfg.get("streamers", [])
    list_ids = set()
    rank = {}                                   # broadcaster_id -> position in your ranked list (0 = top)
    live_ids = set()                            # list streamers live in this game right now
    if broadcasters:
        users = twitch_get(cfg, token, "users", [("login", b.lower()) for b in broadcasters])
        list_ids = {u["id"] for u in users}
        order = {b.lower(): i for i, b in enumerate(broadcasters)}
        rank = {u["id"]: order.get(u["login"].lower(), 999) for u in users}
        if game_id and users:
            live = twitch_get(cfg, token, "streams", [("user_id", u["id"]) for u in users])
            live_ids = {st["user_id"] for st in live if st.get("game_id") == game_id}
            if live_ids:
                on = sorted((rank[i], st["user_name"]) for st in live for i in [st["user_id"]] if i in live_ids)
                log("ranked streamers live now: " + ", ".join(f"#{r + 1} {n}" for r, n in on))
        for u in users:
            clips += twitch_get(
                cfg, token, "clips",
                {"broadcaster_id": u["id"], "started_at": started_at, "first": 20},
            )
    if game_id:
        # whoever is BIG right now: top live English streams in this game with lots of viewers
        min_viewers = cfg.get("big_streamer_min_viewers", 5000)
        live = twitch_get(cfg, token, "streams",
                          {"game_id": game_id, "language": cfg.get("language", "en"), "first": 100})
        live_big = [st for st in live if st.get("viewer_count", 0) >= min_viewers]
        live_big.sort(key=lambda st: st["viewer_count"], reverse=True)
        live_big = live_big[: cfg.get("max_live_streamers", 15)]
        new_ids = [st["user_id"] for st in live_big if st["user_id"] not in list_ids]
        for uid in new_ids:
            clips += twitch_get(cfg, token, "clips",
                                {"broadcaster_id": uid, "started_at": started_at, "first": 20})
        if cfg.get("trust_big_live", False):           # off: someone not on your list needs a clip that's blowing up
            list_ids |= {st["user_id"] for st in live_big}
        if live_big:
            names = ", ".join(f"{st['user_name']} ({st['viewer_count']:,})" for st in live_big[:8])
            log(f"big live streamers right now: {names}")

        clips += twitch_get(
            cfg, token, "clips",
            {"game_id": game_id, "started_at": started_at, "first": 50},
        )
        # keep only clips of this game, even from the streamer list
        clips = [c for c in clips if c.get("game_id") == game_id]

    # dedupe by id, filter by views, sort best first
    seen, unique = set(), []
    for c in clips:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)
    log(f"clips from Twitch ({cfg.get('game_name')}, last {cfg.get('lookback_hours', 6)}h): {len(unique)}")
    # judge by SPEED, not total: a 20-minute-old clip can't have 1000 views yet.
    # need = min_views scaled by age (full amount once the clip is `ramp_hours` old), never below the floor
    now = datetime.now(timezone.utc)
    ramp = cfg.get("ramp_hours", 3)
    floor = cfg.get("min_fresh_views", 150)
    for c in unique:
        try:
            made = datetime.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_h = max(0.1, (now - made).total_seconds() / 3600)
        except Exception:
            age_h = ramp
        c["_age_h"] = age_h
        c["_vph"] = c["view_count"] / age_h                    # views per hour = how hot it is
        c["_need"] = max(floor, cfg.get("min_views", 1000) * min(1.0, age_h / ramp))
    unique = [c for c in unique if c["view_count"] >= c["_need"]]
    log(f"  after min views (scaled by age, {floor}+ when fresh): {len(unique)}")
    # big names first: streamers on your list always count, anyone else only if the clip is blowing up
    viral = cfg.get("category_min_views", 20000)
    unique = [c for c in unique if c.get("broadcaster_id") in list_ids or c["view_count"] >= viral
              or c["_vph"] >= viral / ramp]
    log(f"  after big-streamer filter (your list, or {viral}+ views): {len(unique)}")
    max_len = cfg.get("max_clip_seconds", 30)
    unique = [c for c in unique if 5 <= c.get("duration", 0) <= max_len]
    log(f"  after length (5-{max_len}s): {len(unique)}")
    lang = cfg.get("language", "en")
    if lang:
        unique = [c for c in unique if c.get("language", "").startswith(lang)]
        log(f"  after language ({lang}): {len(unique)}")
    # hierarchy: ranked + live first (in rank order), then ranked but offline, then everyone else
    def priority(c):
        bid = c.get("broadcaster_id")
        if bid in rank and bid in live_ids:
            tier = 0
        elif bid in rank:
            tier = 1
        else:
            tier = 2
        c["_tier"], c["_rank"] = tier, rank.get(bid, 999)
        return (tier, rank.get(bid, 999), -c.get("_vph", c["view_count"]))
    unique.sort(key=priority)
    return unique


# ---------------- Download + convert ----------------

_FFMPEG = None

def ffmpeg_path():
    """Use system ffmpeg if present, else the pip-installed static build."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    import shutil
    p = shutil.which("ffmpeg")
    if not p:
        from static_ffmpeg import run
        p, _ = run.get_or_fetch_platform_executables_else_raise()
    _FFMPEG = p
    return p


def vod_piece(video_id, a, b, out, timeout=90):
    """Download just seconds a..b of a Twitch stream recording: find the few small video pieces
    that cover that time and fetch only those (fast, even for a 12-hour stream)."""
    import time
    t0 = time.time()
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-g",
                        "-f", "best[height<=1080]/best", f"https://www.twitch.tv/videos/{video_id}"],
                       capture_output=True, text=True, timeout=60)
    url = (r.stdout.strip().splitlines() or [""])[0]
    if r.returncode != 0 or ".m3u8" not in url:
        raise RuntimeError("no stream recording link")
    pl = requests.get(url, timeout=30).text
    base = url.rsplit("/", 1)[0] + "/"
    t, dur, picks, first_start = 0.0, None, [], None
    for line in pl.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            if t + dur > a and t < b:
                if first_start is None:
                    first_start = t
                picks.append(line if line.startswith("http") else base + line)
            t += dur
            dur = None
            if t >= b:
                break
    if not picks:
        raise RuntimeError("clip time not in the recording yet")
    joined = out.with_suffix(".ts")
    with open(joined, "wb") as f:
        for u in picks:
            if time.time() - t0 > timeout:
                raise RuntimeError("recording download too slow")
            seg = requests.get(u, timeout=30)
            seg.raise_for_status()
            f.write(seg.content)
    subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-ss", f"{a - first_start:.2f}", "-i", str(joined),
                    "-t", f"{b - a:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k", str(out)], check=True, timeout=180)
    joined.unlink(missing_ok=True)


def download_clip(clip, cfg=None):
    """Download the clip. If the stream recording (VOD) exists, grab the clip PLUS extra seconds
    after it, so a reaction that kept going past where the clipper stopped isn't lost."""
    cfg = cfg or {}
    out = DOWNLOADS / f"{clip['id']}.mp4"
    if out.exists():
        return out
    extra, before = cfg.get("extend_after_seconds", 20), cfg.get("extend_before_seconds", 2)
    off, vid = clip.get("vod_offset"), clip.get("video_id")
    if extra and vid and off is not None:
        a = max(0, int(off) - before)
        b = int(off) + int(round(clip.get("duration", 30))) + extra
        try:
            vod_piece(vid, a, b, out, cfg.get("vod_timeout", 90))
            if out.exists() and out.stat().st_size > 200_000:
                start = off - a                                   # where the original clip starts in our file
                clip["_window"] = (start, start + clip.get("duration", 30))
                log(f"  got clip + {extra}s after it from the stream recording")
                return out
        except Exception as e:
            log(f"  (couldn't use the stream recording: {str(e)[:100]}; using the plain clip)")
        out.unlink(missing_ok=True)
        out.with_suffix(".ts").unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-o", str(out), clip["url"]]
    subprocess.run(cmd, check=True, timeout=300)
    clip.pop("_window", None)
    return out


def find_font():
    for p in [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if Path(p).exists():
            return p
    return None


def clip_loudness(src):
    """Mean volume in dB — louder clip = more chaotic moment."""
    try:
        r = subprocess.run(
            [ffmpeg_path(), "-i", str(src), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        for line in r.stderr.splitlines():
            if "mean_volume" in line:
                return float(line.split("mean_volume:")[1].split("dB")[0])
    except Exception:
        pass
    return None


def track_duration(path):
    """Length of an audio file in seconds, or None if unreadable."""
    try:
        r = subprocess.run([ffmpeg_path(), "-i", str(path)], capture_output=True, text=True, timeout=30)
        for line in r.stderr.splitlines():
            if "Duration:" in line:
                h, m, sec = line.split("Duration:")[1].split(",")[0].strip().split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
    except Exception:
        pass
    return None


def pick_music(src=None):
    """Random track, but matched to clip energy: loud clip -> hype_ track, quiet -> chill_."""
    music_dir = BASE / "music"
    if not music_dir.exists():
        return None
    tracks = [
        f for f in music_dir.iterdir()
        if f.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac")
        and f.stat().st_size > 100_000          # skip broken/empty downloads
        and track_duration(f) is not None       # skip files ffmpeg can't read
    ]
    if not tracks:
        return None
    hype = [t for t in tracks if t.name.lower().startswith("hype_")]
    chill = [t for t in tracks if t.name.lower().startswith("chill_")]
    if src is not None and hype and chill:
        vol = clip_loudness(src)
        if vol is not None:
            pool = hype if vol > -20 else chill
            return random.choice(pool)
    return random.choice(tracks)


# ---------------- Captions ----------------

_TRANSCRIBE_SCRIPT = r"""
import json, subprocess, sys
import numpy as np
from faster_whisper import WhisperModel
src, ffmpeg = sys.argv[1], sys.argv[2]
# decode audio with ffmpeg ourselves (16 kHz mono) instead of faster-whisper's own decoder
raw = subprocess.run([ffmpeg, "-v", "error", "-i", src, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                     capture_output=True, check=True).stdout
audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
m = WhisperModel("base", device="cpu", compute_type="int8")
segs, _ = m.transcribe(audio, word_timestamps=True, language="en")
out = []
try:
    for seg in segs:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                out.append([t, w.start, w.end])
except Exception as e:
    # whisper sometimes trips on a weird bit of audio; keep the words we already got
    sys.stderr.write(f"partial transcript: {type(e).__name__}: {e}\n")
print(json.dumps(out))
"""


def transcribe_words(src, timeout=240):
    """Speech-to-text in a separate process with a time limit, so a hang can't stall the bot."""
    r = subprocess.run(
        [sys.executable, "-c", _TRANSCRIBE_SCRIPT, str(src), ffmpeg_path()],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "transcribe failed")
    return [tuple(w) for w in json.loads(r.stdout.strip().splitlines()[-1])]


SWEARS = ["fuck", "shit", "bitch", "nigga", "nigger", "cunt", "pussy", "whore", "slut", "fag"]


def clean_word(w):
    """F***ING instead of the full swear (keeps reach up on YouTube/Instagram)."""
    def bleep(m):
        word = m.group(0)
        return word[0] + "*" * (len(word) - 1)
    pat = r"\b\w*(" + "|".join(SWEARS) + r")\w*"
    return re.sub(pat, bleep, w, flags=re.IGNORECASE)


def _ass_time(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(words, out_path):
    """Chunked pop-in captions: white bold, longest word yellow."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour,"
        " Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, BorderStyle\n"
        "Style: Cap,Anton,136,&H00FFFFFF,&H00000000,&H80000000,"
        "-1,7,3,2,60,60,480,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
    )
    lines = []
    chunk = []
    for i, (w, s, e) in enumerate(words):
        chunk.append((w, s, e))
        gap_next = i + 1 < len(words) and words[i + 1][1] - e > 0.6
        if len(chunk) == 3 or gap_next or i == len(words) - 1:
            cs, ce = chunk[0][1], chunk[-1][2]
            if ce - cs < 0.25:
                ce = cs + 0.25
            longest = max(range(len(chunk)), key=lambda j: len(chunk[j][0]))
            parts = []
            for j, (cw, _, _) in enumerate(chunk):
                txt = clean_word(cw).upper().replace("{", "").replace("}", "")
                if j == longest and len(chunk) > 1:
                    parts.append(r"{\c&H00DFFF&}" + txt + r"{\c&HFFFFFF&}")
                else:
                    parts.append(txt)
            anim = r"{\fscx80\fscy80\t(0,90,\fscx106\fscy106)\t(90,150,\fscx100\fscy100)}"
            lines.append(
                f"Dialogue: 0,{_ass_time(cs)},{_ass_time(ce)},Cap,{anim}{' '.join(parts)}"
            )
            chunk = []
    out_path.write_text(header + "\n".join(lines))
    return out_path


def game_subtitle_profile(src):
    """For every 0.1 s: is the GAME showing its own subtitle (white text, bottom middle)?
    GTA prints subtitles whenever a character talks, so that's game dialogue, not the streamer."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W, H = int(cap.get(3)), int(cap.get(4))
    if not W or not H:
        return []
    step = max(1, int(round(fps / 5)))                   # look 5 times a second
    y0, y1, x0, x1 = int(H * 0.86), int(H * 0.97), int(W * 0.2), int(W * 0.8)
    scale = 1080 / H
    out, i, last = [], 0, False
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            ok, f = cap.retrieve()
            if not ok:
                break
            band = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            if scale != 1:
                band = cv2.resize(band, (0, 0), fx=scale, fy=scale)
            n, _, st, _ = cv2.connectedComponentsWithStats((band > 190).astype(np.uint8))
            ys = [sy + sh / 2 for sx, sy, sw, sh, sa in st[1:] if 3 <= sh <= 30 and sw <= 40 and sa >= 4]
            if len(ys) >= 6:                              # letters sitting on one line = a subtitle
                mid = sorted(ys)[len(ys) // 2]
                last = sum(1 for y in ys if abs(y - mid) <= 14) >= 6
            else:
                last = False
        t = i / fps
        while len(out) < int(t * 10) + 1:
            out.append(last)
        i += 1
    cap.release()
    return out


def streamer_words_only(words, src, clip, cfg, quiet=False):
    """Drop lines spoken by the game (cutscenes, NPCs). Trick: when the streamer talks, their
    face on the cam moves; when a game character talks, the streamer is usually just watching."""
    if not words or not cfg.get("skip_game_captions", True):
        return words
    subs = game_subtitle_profile(src)
    cam = clip.get("_cam")
    if not cam:                                        # no face-cam: game subtitles are our only clue
        is_game = lambda avg: False
        motion, base = [], 1
        return _drop_game_lines(words, subs, motion, is_game, quiet)
    face = clip.get("_face")
    if face:                                           # watch the MOUTH: talking moves it, watching doesn't
        fx, fy, fw, fh = face
        region = (int(fx + fw * 0.2) // 2 * 2, int(fy + fh * 0.55) // 2 * 2,
                  max(8, int(fw * 0.6) // 2 * 2), max(8, int(fh * 0.45) // 2 * 2))
        motion = motion_profile(src, region)
        if len(motion) < 10:
            return words
        base = sorted(motion)[len(motion) // 4] or 1e-6       # mouth at rest
        factor = cfg.get("mouth_talk_factor", 1.2)            # careful: head/hand moves fool this
        is_game = lambda avg: avg < base * factor
    else:
        x, y, w, h, _, _ = cam
        motion = motion_profile(src, (x, y, w, h))
        if len(motion) < 10:
            return words
        base = sorted(motion)[len(motion) // 2] or 1e-6       # typical face movement in this clip
        factor = cfg.get("game_voice_factor", 0.6)
        is_game = lambda avg: avg < base * factor
    return _drop_game_lines(words, subs, motion, is_game, quiet)


def _drop_game_lines(words, subs, motion, is_game, quiet):
    """Split words into lines (pauses) and drop the ones the game said."""
    lines, cur = [], []
    for wd in words:
        if cur and wd[1] - cur[-1][2] > 0.6:
            lines.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        lines.append(cur)
    keep, dropped = [], 0
    for ln in lines:
        a, b = int(ln[0][1] * 10), int(ln[-1][2] * 10) + 1
        sub = subs[a:b]
        if sub and sum(sub) / len(sub) >= 0.4:          # game subtitle on screen = a character said it
            dropped += 1
            continue
        seg = motion[a:b]
        if seg and is_game(sum(seg) / len(seg)):         # streamer's mouth/face still = not them
            dropped += 1
            continue
        keep += ln
    if dropped and not quiet:
        log(f"  captions: skipped {dropped} game-voice line(s)")
    return keep


def make_captions(src, clip, cfg):
    """Returns path to .ass subtitle file, or None."""
    if not cfg.get("captions", True):
        return None
    if not clip.get("_cam") and not cfg.get("captions_without_cam", False):
        log("  captions: off (no face-cam, can't tell the streamer from game voices)")
        return None
    try:
        words = transcribe_words(src, cfg.get("caption_timeout", 240))
        words = streamer_words_only(words, src, clip, cfg)
        if not words:
            return None
        ass = SHORTS / f"{clip['id']}.ass"
        return build_ass(words, ass)
    except Exception as e:
        log(f"captions skipped ({e})")
        return None


def rms_profile(src, win_s=0.1):
    """Loudness of each 0.1 s slice of the clip's audio."""
    import array, math
    try:
        r = subprocess.run(
            [ffmpeg_path(), "-v", "error", "-i", str(src), "-ac", "1", "-ar", "8000",
             "-f", "s16le", "-"],
            capture_output=True, timeout=120,
        )
        samples = array.array("h", r.stdout)
    except Exception:
        return []
    win = int(8000 * win_s)
    out = []
    for i in range(0, len(samples) - win, win):
        chunk = samples[i:i + win]
        out.append(math.sqrt(sum(x * x for x in chunk) / win))
    return out


def motion_profile(src, crop=None):
    """How much the picture changes every 0.1 s (0 = frozen). crop=(x, y, w, h) to only watch
    one area, e.g. the streamer's face-cam (big jump = big physical reaction)."""
    try:
        import numpy as np
        vf = "fps=10"
        if crop:
            x, y, w, h = crop
            vf += f",crop={w}:{h}:{x}:{y}"
        vf += ",scale=96:54,format=gray"
        r = subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(src), "-vf", vf,
                            "-f", "rawvideo", "-"], capture_output=True, timeout=180)
        frames = np.frombuffer(r.stdout, np.uint8)
        n = len(frames) // (96 * 54)
        if n < 3:
            return []
        frames = frames[: n * 96 * 54].reshape(n, 54, 96).astype(np.float32)
        diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
        return [0.0] + [float(v) for v in diff]
    except Exception:
        return []


def spike(values, around=None, window=15):
    """How much the biggest jump stands out from normal (1 = no spike). Optionally only
    look within `window` steps of index `around` (e.g. near the loud moment)."""
    if len(values) < 5:
        return 1.0
    mean = sum(values) / len(values) or 1e-6
    vals = values
    if around is not None:
        vals = values[max(0, around - window): around + window] or values
    return max(vals) / mean


def window_range(rms, window):
    """Indices (0.1 s steps) of the original Twitch clip inside our (maybe longer) download."""
    if not window:
        return range(len(rms))
    a, b = max(0, int(window[0] * 10)), min(len(rms), int(window[1] * 10))
    return range(a, b) if b - a > 10 else range(len(rms))


def find_peak(src, window=None):
    """Time (s) of the loudest moment (inside the original clip), or None if no clear spike."""
    rms = rms_profile(src)
    if len(rms) < 10:
        return None
    mean = sum(rms) / len(rms)
    peak_i = max(window_range(rms, window), key=lambda i: rms[i])
    if mean == 0 or rms[peak_i] / mean < 1.8:
        return None  # no real spike, effects would look random
    return max(0.0, peak_i * 0.1 - 0.05)


def snap_to_speech(start, end, words, dur, cfg):
    """Don't start or end in the middle of someone talking: let the sentence finish."""
    if not words:
        return start, end
    gap = 0.7                                         # a pause this long = sentence over
    max_extra = cfg.get("max_finish_seconds", 8)      # never drag on more than this past the payoff window
    limit = min(dur, end + max_extra)
    talking = True
    while talking:
        talking = False
        for w, a, b in words:
            if a < end + gap and b > end and b <= limit:   # someone still talking at the cut
                end = min(limit, b + 0.4)
                talking = True
    for w, a, b in words:                             # start at the beginning of a sentence
        if a < start < b + gap:
            start = max(0.0, a - 0.2)
    return round(start, 2), round(end, 2)


def let_it_finish(end, rms, dur, cfg, motion=None, face=None):
    """Keep going until the moment is REALLY over. Funny can be quiet (a frozen stare, silent
    wheezing, the game doing something dumb), so we only stop when ALL THREE are calm for ~1.5 s:
    the sound, the streamer's face-cam, and the gameplay. No such dead stretch = play to the end
    (the person who made the Twitch clip already chose a good ending)."""
    if not rms:
        return end
    def calm_line(vals, factor):
        if not vals or len(vals) < 10:
            return None
        busy = sorted(vals)[int(len(vals) * 0.75)] or 1e-6   # how much it moves when stuff happens
        return [v < busy * factor for v in vals]
    mean = sum(rms) / len(rms) or 1
    sound_calm = [v < mean * cfg.get("still_funny_level", 0.9) for v in rms]
    game_calm = calm_line(motion, 0.5)
    face_calm = calm_line(face, 0.5)
    need = int(cfg.get("dead_seconds_to_stop", 1.5) * 10)
    run = 0
    for i in range(int(end * 10), len(rms)):
        calm = sound_calm[i]
        if game_calm is not None and i < len(game_calm):
            calm = calm and game_calm[i]
        if face_calm is not None and i < len(face_calm):
            calm = calm and face_calm[i]
        run = run + 1 if calm else 0
        if run >= need:                                  # truly dead: end just after it went calm
            stop = (i - run + 1) * 0.1 + 0.6
            stop = max(end, min(dur, stop))
            return dur if dur - stop < cfg.get("play_to_end_seconds", 4) else round(stop, 2)
    return dur                                           # never went fully dead: play the whole thing


def plan_cuts(rms, peak, cfg, words=None, motion=None, face=None):
    """Which parts of the clip to keep: short build-up, the payoff, the reaction; no boring bits.
    Boring = dead quiet, OR fairly quiet while nothing much moves on screen. Speech is never cut."""
    dur = len(rms) * 0.1
    pre = cfg.get("max_buildup_seconds", 10)
    post = cfg.get("after_payoff_seconds", 6)
    start, end = max(0.0, peak - pre), min(dur, peak + post)
    start, end = snap_to_speech(start, end, words, dur, cfg)
    end = let_it_finish(end, rms, dur, cfg, motion, face)
    start, end = snap_to_speech(start, end, words, dur, cfg)

    # dead air = quiet stretches before the payoff (quiet = under 35% of the clip's average loudness)
    mean = sum(rms) / len(rms)
    quiet = [v < mean * 0.35 for v in rms]
    if motion and len(motion) > 5:
        mmean = sum(motion) / len(motion) or 1e-6
        for k in range(len(quiet)):
            mv = motion[k] if k < len(motion) else mmean
            if rms[k] < mean * 0.6 and mv < mmean * 0.45:   # quiet-ish AND nothing happening
                quiet[k] = True
    for w, a, b in words or []:                       # speech is never dead air
        for k in range(max(0, int(a * 10)), min(len(quiet), int(b * 10) + 1)):
            quiet[k] = False
    min_gap = cfg.get("min_dead_air_seconds", 0.8)
    keep, cur = [], start
    i = int(start * 10)
    stop = int(max(start, peak - 0.5) * 10)          # never cut the moment right before the payoff
    while i < stop:
        if quiet[i]:
            j = i
            while j < stop and quiet[j]:
                j += 1
            if (j - i) * 0.1 >= min_gap:              # long enough to be boring: cut it (keep 0.15 s each side)
                a, b = i * 0.1 + 0.15, j * 0.1 - 0.15
                if a > cur:
                    keep.append((cur, a))
                cur = b
            i = j
        else:
            i += 1
    keep.append((cur, end))
    keep = [(round(a, 2), round(b, 2)) for a, b in keep if b - a >= 0.3]
    total = sum(b - a for a, b in keep)
    return keep if total >= cfg.get("min_short_seconds", 6) else [(round(start, 2), round(end, 2))]


def tighten_clip(src, clip, cfg):
    """Story cut: setup -> (skip boring) -> it happens -> (skip) -> payoff -> reaction.
    Optionally opens with a 1-second teaser of the payoff so people stay to see how it happened."""
    if not cfg.get("jump_cuts", True):
        return src
    rms = rms_profile(src)
    window = clip.get("_window")
    peak = find_peak(src, window)
    if len(rms) < 20:
        return src
    if peak is None:
        if not window:
            return src                                # no clear payoff: leave the clip alone
        peak = (window[0] + window[1]) / 2            # extended download: centre on the original clip
    try:
        words = transcribe_words(src, cfg.get("caption_timeout", 240))
    except Exception:
        words = []
    motion = motion_profile(src)
    face = []
    if clip.get("_cam"):                                 # the streamer's face-cam movement
        x, y, w, h, _, _ = clip["_cam"]
        face = motion_profile(src, (x, y, w, h))
    keep = plan_cuts(rms, peak, cfg, words, motion, face)
    dur = len(rms) * 0.1

    # teaser: flash the payoff first ("how did we get here"), only if the payoff is far enough in
    teaser = None
    kept_before_peak = sum(min(b, peak) - a for a, b in keep if a < peak)
    if cfg.get("teaser", True) and kept_before_peak >= cfg.get("teaser_min_buildup", 4):
        t0 = max(0.0, peak - 0.25)
        teaser = (round(t0, 2), round(min(dur, t0 + cfg.get("teaser_seconds", 1.2)), 2))

    if not teaser and len(keep) == 1 and keep[0][0] < 0.3 and keep[0][1] > dur - 0.3:
        return src                                    # nothing worth cutting
    pieces = ([teaser] if teaser else []) + keep
    parts, labels = [], []
    for k, (a, b) in enumerate(pieces):
        parts.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{k}];"
                     f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{k}]")
        labels.append(f"[v{k}][a{k}]")
    fc = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(pieces)}:v=1:a=1[v][a]"
    out = DOWNLOADS / f"{clip['id']}_cut.mp4"
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", fc,
           "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(out)]
    subprocess.run(cmd, check=True, timeout=600)
    new_len = sum(b - a for a, b in pieces)
    log(f"  story cut: {dur:.0f}s -> {new_len:.0f}s ({len(keep)} pieces{', teaser opening' if teaser else ''})")
    return out


# ---------------- Nudity check ----------------

NUDE_CLASSES = {"BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
                "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}   # underwear shows up as *_COVERED = fine
_NUDE = None


def has_nudity(src, cfg):
    """True if the clip shows full nudity in 2+ frames (checked twice a second)."""
    global _NUDE
    import shutil, tempfile
    try:
        from nudenet import NudeDetector
    except Exception:
        return False                                   # tool missing: don't block posting
    if _NUDE is None:
        _NUDE = NudeDetector()
    tmp = Path(tempfile.mkdtemp())
    try:
        subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(src), "-vf", "fps=2,scale=-2:720",
                        str(tmp / "f%03d.jpg")], check=True, timeout=180)
        min_score = cfg.get("nudity_min_score", 0.5)
        hits = 0
        for f in sorted(tmp.glob("*.jpg")):
            if any(d["class"] in NUDE_CLASSES and d["score"] >= min_score for d in _NUDE.detect(str(f))):
                hits += 1
                if hits >= cfg.get("nudity_min_frames", 2):
                    return True
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def nudity_fraction(src, cfg):
    """Share of the clip (0-1) that shows full nudity, checked twice a second."""
    global _NUDE
    import shutil, tempfile
    try:
        from nudenet import NudeDetector
    except Exception:
        return 0.0
    if _NUDE is None:
        _NUDE = NudeDetector()
    tmp = Path(tempfile.mkdtemp())
    try:
        subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(src), "-vf", "fps=2,scale=-2:720",
                        str(tmp / "f%03d.jpg")], check=True, timeout=180)
        frames = sorted(tmp.glob("*.jpg"))
        if not frames:
            return 0.0
        min_score = cfg.get("nudity_min_score", 0.5)
        hits = sum(1 for f in frames
                   if any(d["class"] in NUDE_CLASSES and d["score"] >= min_score for d in _NUDE.detect(str(f))))
        return hits / len(frames)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def censor_clip(src, clip, cfg):
    """Pixelate exposed body parts frame by frame (underwear is left alone).
    Returns the censored file, or src unchanged if nothing needed covering."""
    global _NUDE
    import cv2
    from nudenet import NudeDetector
    if _NUDE is None:
        _NUDE = NudeDetector()
    min_score = cfg.get("nudity_min_score", 0.5)
    every = cfg.get("censor_every_frames", 3)          # check every 3rd frame, hold the box in between
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W, H = int(cap.get(3)), int(cap.get(4))
    tmp = DOWNLOADS / f"{clip['id']}_censor_tmp.mp4"
    out = DOWNLOADS / f"{clip['id']}_censored.mp4"
    wr = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
    boxes, prev, i, covered = [], [], 0, 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % every == 0:
            prev, boxes = boxes, []
            for d in _NUDE.detect(frame):
                if d["class"] in NUDE_CLASSES and d["score"] >= min_score:
                    boxes.append(d["box"])               # [x, y, w, h]
        for (x, y, w, h) in boxes + prev:                # last two checks together = no flicker
            px, py = int(w * 0.3), int(h * 0.3)          # a bit bigger than the detection
            x0, y0 = max(0, int(x) - px), max(0, int(y) - py)
            x1, y1 = min(W, int(x + w) + px), min(H, int(y + h) + py)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            roi = frame[y0:y1, x0:x1]
            small = cv2.resize(roi, (max(1, (x1 - x0) // 24), max(1, (y1 - y0) // 24)))
            frame[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
        if boxes or prev:
            covered += 1
        wr.write(frame)
        i += 1
    cap.release()
    wr.release()
    if covered == 0:
        tmp.unlink(missing_ok=True)
        return src
    subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(tmp), "-i", str(src),
                    "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "copy", "-shortest", str(out)], check=True, timeout=600)
    tmp.unlink(missing_ok=True)
    log(f"  censored nudity in {covered / fps:.1f}s of the clip")
    return out


# ---------------- Is this clip actually good? ----------------

FUNNY_WORDS = ["haha", "hahaha", "lmao", "lol", "bro", "bruh", "nah", "why", "wtf", "yo",
               "what", "dude", "stop", "help", "noo", "dead", "crying", "chat", "dawg", "bro what",
               "what the", "are you serious", "i can't", "this game", "who did", "huh"]
FAIL_WORDS = ["wasted", "busted", "died", "dead", "crash", "crashed", "arrested", "cops", "police",
              "oh no", "no no", "noo", "i'm done", "it's over", "why would", "not again", "run"]
HYPE_WORDS = ["let's go", "lets go", "no way", "oh my god", "omg", "clutch", "insane", "gg",
              "we did it", "finally", "first try", "yes", "easy", "cracked", "goated", "let's gooo"]
TITLE_FUNNY = ["lol", "lmao", "funny", "fail", "dies", "died", "crash", "💀", "😭", "bro", "why", "wtf", "rip",
               "arrested", "busted", "wasted", "cops", "glitch", "troll", "betray"]
TITLE_HYPE = ["insane", "clutch", "first try", "no way", "crazy", "best", "perfect", "world record", "god"]


def best_quote(words, peak):
    """The streamer's own punchline: a short line said right around the big moment.
    e.g. 'BRO WHAT IS THIS GAME'. Returns '' if nothing quotable."""
    if not words or peak is None:
        return ""
    lines, cur = [], []
    for i, (w, a, b) in enumerate(words):
        if cur and a - cur[-1][2] > 0.6:             # pause = new line
            lines.append(cur)
            cur = []
        cur.append((w, a, b))
    if cur:
        lines.append(cur)
    best, best_score = "", 0.0
    for ln in lines:
        text = " ".join(w for w, _, _ in ln).strip(" .,!?")
        n = len(text.split())
        if not 2 <= n <= 8:
            continue
        start, end = ln[0][1], ln[-1][2]
        near = 1.0 / (1.0 + abs((start + end) / 2 - peak))     # closer to the payoff = better
        low = " " + text.lower() + " "
        punch = sum(low.count(f" {k}") for k in FUNNY_WORDS + FAIL_WORDS + HYPE_WORDS)
        sc = near * 3 + punch + (1 if 3 <= n <= 6 else 0)
        if sc > best_score:
            best, best_score = text, sc
    return clean_word(best) if best_score >= 1.5 else ""


def score_moment(raw, clip, cfg):
    """Free heuristic for the 7 things that make a clip funny. Returns (score, category)."""
    import math
    text, words, heard = "", [], False
    try:
        words = transcribe_words(raw, cfg.get("caption_timeout", 240))
        heard = True
    except Exception as e:
        log(f"  (scoring without speech: {e})")
    # find the face-cam first, so we only count what the STREAMER says (not game characters)
    clip["_cam"] = find_facecam(raw) if cfg.get("layout", "split") == "split" else None
    clip["_face"] = _LAST_FACE if clip["_cam"] else None
    all_words = len(words)
    words = streamer_words_only(words, raw, clip, cfg, quiet=True)
    if all_words > len(words):
        log(f"  ({all_words - len(words)} of {all_words} words were the game talking, ignored)")
    text = " " + " ".join(w for w, _, _ in words).lower() + " "
    clip["_said"] = " ".join(w for w, _, _ in words) if clip["_cam"] else ""
    title = clip.get("title", "").lower()
    funny = sum(text.count(f" {w}") for w in FUNNY_WORDS) + 2 * sum(k in title for k in TITLE_FUNNY)
    fail = sum(text.count(f" {w}") for w in FAIL_WORDS)                       # 3. fails / karma
    hype = sum(text.count(f" {w}") for w in HYPE_WORDS) + 2 * sum(k in title for k in TITLE_HYPE)

    rms = rms_profile(raw)
    excite, peak_i = 0.0, None
    if len(rms) >= 10:
        mean = sum(rms) / len(rms) or 1
        rng = window_range(rms, clip.get("_window"))         # judge the original clip part
        peak_i = max(rng, key=lambda i: rms[i])
        excite = max(0.0, rms[peak_i] / mean - 1.5)           # 1. yelling / screaming
        excite += sum(1 for v in rms if v > mean * 2.2) / 10   # how long it stays loud

    # 1. big physical reaction: the face-cam suddenly moves a lot near the loud moment
    reaction = 0.0
    if clip["_cam"]:
        x, y, w, h, _, _ = clip["_cam"]
        cam_motion = motion_profile(raw, (x, y, w, h))
        reaction = max(0.0, spike(cam_motion, peak_i) - 2.0)
    # 2. something unexpected: the game picture suddenly changes a lot (crash, explosion, chaos)
    chaos = max(0.0, spike(motion_profile(raw), peak_i) - 2.5)
    # 4. chaos with friends: lots of talking packed into the clip
    dur = len(rms) * 0.1 or 1
    density = len(words) / dur                             # words per second
    group = max(0.0, density - 2.5)
    # 5. a quotable one-liner near the payoff
    clip["_quote"] = best_quote(words, peak_i * 0.1 if peak_i is not None else None) if clip["_cam"] else ""
    quote = 3.0 if clip["_quote"] else 0.0
    # the streamer's voice is half the joke: silent clips lose, talking / yelling clips win
    talk_secs = sum(b - a for _, a, b in words)
    if heard and (len(words) < cfg.get("min_words", 5) or talk_secs < 2.0):
        talking = -cfg.get("silent_penalty", 8)          # barely says anything
    else:
        talking = min(len(words) / 10, 2.0) + (1.5 if excite >= 1.5 and words else 0)
    if not clip["_cam"]:
        talking = min(talking, 0.0)                    # no cam: those words might all be the game

    moment = (1.5 * min(funny, 8) + 2.0 * min(fail, 5) + 1.5 * min(hype, 8) + 2 * min(excite, 4)
              + 1.5 * min(reaction, 4) + 1.0 * min(chaos, 4) + 1.0 * min(group, 3) + quote + talking)
    if (funny == 0 and hype == 0 and fail == 0 and excite < 0.5 and reaction < 0.5) or (heard and talking < 0 and reaction < 1):
        category = "boring"
    elif hype > funny + fail:
        category = "achievement"
    else:
        category = "funny"
    ladder = {0: 4, 1: 1.5}.get(clip.get("_tier", 2), 0)        # 6. ranked + live streamers get a head start
    ladder += max(0, 2 - clip.get("_rank", 999) * 0.1)
    cam_bonus = cfg.get("facecam_bonus", 4) if clip["_cam"] else 0
    score = moment + ladder + cam_bonus + math.log10(max(clip.get("_vph", clip["view_count"]), 1))
    clip["_why"] = (f"talk {talking:+.1f} ({len(words)} words), react {reaction:.1f}, chaos {chaos:.1f}, fail {fail}, funny {funny}, "
                    f"hype {hype}, loud {excite:.1f}" + (f', quote "{clip["_quote"]}"' if clip["_quote"] else ""))
    return round(score, 1), category


GENERIC_TITLES = {"", "lol", "lmao", "w", "l", "clip", "omg", "bruh", "wtf", "xd", "kek",
                  "pog", "poggers", "lul", "omegalul", "kekw", "haha", "wow", "nice", "gg"}

ACHIEVEMENT_HOOKS = [
    "{name} ACTUALLY DID IT",
    "NO WAY HE PULLED THIS OFF",
    "THIS WAS INSANE",
    "{name} WENT CRAZY HERE",
    "FIRST TRY?!",
]

HOOK_TEMPLATES = [
    "WAIT FOR IT...",
    "{name} WAS NOT READY FOR THIS",
    "THIS WENT HORRIBLY WRONG",
    "{name} CAN'T BELIEVE WHAT JUST HAPPENED",
    "WATCH TILL THE END",
    "{name} LOST IT",
]


def pick_hook(clip):
    """Returns (HOOK IN CAPS for the video, Title text in normal casing). Picked once per clip."""
    import re
    if "_hook" in clip:
        return clip["_hook"], clip["_title_text"]
    display = re.sub(r"[^A-Za-z0-9_ ]", "", clip.get("broadcaster_name", "")) or "He"
    title = clip.get("title", "")
    title = re.sub(r"https?://\S+", "", title)                 # drop links
    title = re.sub(r"[^\x20-\x7E]", "", title)                 # drop emojis / non-latin
    title = re.sub(r"\s+", " ", title).strip(" -|:")
    words = title.split()
    lowered = re.sub(r"[^a-z ]", "", title.lower()).strip()
    if len(words) >= 3 and lowered not in GENERIC_TITLES:
        text = title.lower() if title.isupper() else title     # tame ALL-CAPS Twitch titles
        text = text[0].upper() + text[1:]
        if display != "He":   # keep the streamer's own spelling (xQc, not Xqc)
            text = re.sub(rf"\b{re.escape(display)}\b", display, text, flags=re.IGNORECASE)
        hook = title.upper()
        if len(hook) > 60:
            hook = hook[:57].rsplit(" ", 1)[0] + "..."
    elif clip.get("_quote"):                                   # streamer's own punchline
        q = clip["_quote"]
        hook = f'"{q.upper()}"'
        text = f'{display}: "{q[0].upper() + q[1:]}"'
    else:
        pool = ACHIEVEMENT_HOOKS if clip.get("_category") == "achievement" else HOOK_TEMPLATES
        t = random.choice(pool)
        hook = t.format(name=display.upper())
        text = t.format(name="{N}").lower()
        text = (text[0].upper() + text[1:]).replace("{n}", display)
    hook, text = clean_word(hook), clean_word(text)
    clip["_hook"], clip["_title_text"] = hook, text
    return hook, text


def make_hook(clip):
    return pick_hook(clip)[0]


def build_top_ass(clip, out_path, split=False, hook=True):
    """Streamer name (small) + hook line (big, wraps to 2 lines) in the top blurred area."""
    import re
    def esc(t):
        return t.replace("\\", "").replace("{", "(").replace("}", ")")
    show_hook = hook
    name = re.sub(r"[^A-Za-z0-9_ ]", "", clip.get("broadcaster_name", "")).upper()
    hook = esc(make_hook(clip))
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour,"
        " Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, BorderStyle\n"
        "Style: Name,Anton,68,&H0000DFFF,&H00000000,&H80000000,-1,5,2,2,60,60,340,1\n"
        "Style: Hook,Anton,120,&H00FFFFFF,&H00000000,&H80000000,-1,7,3,8,70,70,190,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
    )
    lines = []
    if name:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Name,@{esc(name)}")
    pop = r"{\fscx70\fscy70\t(0,120,\fscx105\fscy105)\t(120,200,\fscx100\fscy100)}"
    if split:   # sit the hook right on the line between face-cam and gameplay
        pop = r"{\an5\pos(540,672)\fs92}" + pop
    if show_hook:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Hook,{pop}{hook}")
    out_path.write_text(header + "\n".join(lines))
    return out_path


def find_peaks(src, n=3, gap=3.0):
    """Up to n loud moments (seconds), loudest first, at least `gap` apart."""
    rms = rms_profile(src)
    if len(rms) < 10:
        return []
    mean = sum(rms) / len(rms) or 1
    order = sorted(range(len(rms)), key=lambda i: rms[i], reverse=True)
    peaks = []
    for i in order:
        if rms[i] / mean < 1.8:
            break
        t = max(0.0, i * 0.1 - 0.05)
        if all(abs(t - q) >= gap for q in peaks):
            peaks.append(t)
        if len(peaks) >= n:
            break
    return peaks


_LAST_FACE = None


def find_facecam(src):
    """Never let face detection kill a clip: on any error, fall back to the blur layout."""
    try:
        return _find_facecam(src)
    except Exception as e:
        log(f"  (face-cam check failed: {type(e).__name__}: {e})")
        return None


def webcam_box(smalls, face, W, H):
    """Find the webcam's real rectangle: inside it the room barely changes, while the game around it
    keeps changing. Frames are 1/4 size. Returns (x, y, w, h) in full pixels, or None."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    if len(smalls) < 6:
        return None
    sd = np.stack([f.astype(np.float32) for f in smalls]).std(0)
    thr = max(8.0, 0.45 * float(np.median(sd)))
    static = (sd < thr).astype(np.uint8)
    fx, fy, fw, fh = [v / 4 for v in face]
    k = max(3, int(fw * 1.2)) | 1                      # fill the hole where the streamer moves
    closed = cv2.morphologyEx(static, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(closed)
    cx, cy = int(fx + fw / 2), int(fy + fh / 2)
    best = None
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if x <= cx <= x + w and y <= cy <= y + h:
            if best is None or area < st[best][4]:
                best = i
    if best is None:
        return None
    x, y, w, h, area = st[best]
    sh, sw = static.shape
    face_area = fw * fh
    if not (2.5 * face_area <= w * h <= 45 * face_area):   # way too small/big to be a webcam box
        return None
    if w > sw * 0.6 or h > sh * 0.75 or area / (w * h) < 0.6:  # not a solid rectangle
        return None
    if w / max(h, 1) < 0.8 or w / max(h, 1) > 2.6:
        return None
    pad = 0.02                                         # step in a little so the frame's border line is gone
    x, y, w, h = x + w * pad, y + h * pad, w * (1 - 2 * pad), h * (1 - 2 * pad)
    return x * 4, y * 4, w * 4, h * 4


def _find_facecam(src):
    """Find the streamer's webcam: a face that keeps showing up in the same spot, near the
    edges of the screen (game characters move around and sit in the middle; webcams don't).
    Returns (x, y, w, h, W, H) crop box in source pixels, or None."""
    import cv2
    cap = cv2.VideoCapture(str(src))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    W, H = cap.get(3), cap.get(4)
    if n < 10 or not W or not H:
        cap.release()
        return None
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    side = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    min_face = int(H * 0.035)
    samples = 12
    dets, smalls = [], []
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (k + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        smalls.append(cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (0, 0), fx=0.25, fy=0.25))
        found = list(front.detectMultiScale(gray, 1.08, 4, minSize=(min_face, min_face)))
        found += list(side.detectMultiScale(gray, 1.08, 4, minSize=(min_face, min_face)))
        flipped = side.detectMultiScale(cv2.flip(gray, 1), 1.08, 4, minSize=(min_face, min_face))
        found += [(int(W - x - w), y, w, h) for (x, y, w, h) in flipped]   # faces turned the other way
        keep = []
        for (x, y, w, h) in found:
            cx, cy = x + w / 2, y + h / 2
            edge = cx < W * 0.3 or cx > W * 0.7          # webcams live at the sides, characters in the middle
            if edge and w < H * 0.4:
                keep.append((int(x), int(y), int(w), int(h)))
        dets.append(keep)
    cap.release()
    if not dets:
        return None
    best = None
    for f in [f for fs in dets for f in fs]:
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        hits = []
        for fs in dets:
            m = [g for g in fs if abs(g[0] + g[2] / 2 - cx) < W * 0.06 and abs(g[1] + g[3] / 2 - cy) < H * 0.10]
            if m:
                hits.append(m[0])
        if len(hits) >= max(3, int(0.3 * len(dets))) and (best is None or len(hits) > len(best)):
            best = hits
    if not best:
        return None                                   # no steady face = no webcam (or face hidden)
    med = lambda i: sorted(h[i] for h in best)[len(best) // 2]
    fx, fy, fw, fh = med(0), med(1), med(2), med(3)
    box = webcam_box(smalls, (fx, fy, fw, fh), W, H)
    if box:                                            # crop exactly the webcam rectangle (no game bits)
        bx, by, bw, bh = box
        want = 1080 / 672
        if bw / bh > want:                             # too wide: trim the sides, keep the face centred
            cw, ch = bh * want, bh
            x = min(max(bx, fx + fw / 2 - cw / 2), bx + bw - cw)
            y = by
        else:                                          # too tall: trim top/bottom around the face
            cw, ch = bw, bw / want
            x = bx
            y = min(max(by, fy + fh / 2 + fh * 0.3 - ch / 2), by + bh - ch)
    else:
        # no clear webcam rectangle (cut-out cam): crop around the face
        cw = min(W, max(fw * 3.0, min(W * 0.22, fw * 8.0)))
        ch = cw * 672 / 1080
        if ch > H:
            ch, cw = H, H * 1080 / 672
        cx, cy = fx + fw / 2, fy + fh / 2 + fh * 0.3
        x = min(max(0, cx - cw / 2), W - cw)
        y = min(max(0, cy - ch / 2), H - ch)
    ev = lambda v: int(v) // 2 * 2
    box = (ev(x), ev(y), ev(cw), ev(ch))
    # real webcam = a room that barely moves; a game scene keeps moving / cutting between shots
    cam_m, frame_m = motion_profile(src, box), motion_profile(src)
    if cam_m and frame_m:
        med = lambda v: sorted(v)[len(v) // 2]
        if med(cam_m) > 1.3 * max(med(frame_m), 0.3):
            return None                               # that "face" was a game character, not a webcam
    global _LAST_FACE
    _LAST_FACE = (fx, fy, fw, fh)                     # the face itself (for the mouth check)
    return box[0], box[1], box[2], box[3], ev(W), ev(H)


def make_vertical(src, clip, cfg):
    """16:9 clip -> 1080x1920 short: blur bg, peak zoom/shake/flash/boom, captions, banner, music."""
    import re
    out = SHORTS / f"{clip['id']}_short.mp4"

    if "_cam" in clip:                                   # already found while scoring
        cam = clip["_cam"]
    else:
        cam = find_facecam(src) if cfg.get("layout", "split") == "split" else None
    if cam:
        # streamer's face on top, gameplay underneath (reaction + moment at the same time)
        x, y, w, h, W, H = cam
        gw = int(H * 1080 / 1248) // 2 * 2
        gx = int((W - gw) / 2) // 2 * 2
        vf = (
            f"[0:v]split=2[fa][fb];"
            f"[fa]crop={w}:{h}:{x}:{y},scale=1080:672,setsar=1[top];"
            f"[fb]crop={gw}:{H}:{gx}:0,scale=1080:1248,setsar=1[bot];"
            f"[top][bot]vstack=inputs=2,fps=30[v1]"
        )
        log("  layout: split screen (face-cam found)")
    else:
        vf = (
            "[0:v]scale=-2:1920,crop=1080:1920,boxblur=24:6,eq=brightness=-0.08[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30[v1]"
        )
        log("  layout: blur (no face-cam found)")
    clip["_split"] = bool(cam)
    last_v = "[v1]"

    peaks = find_peaks(src, cfg.get("max_booms", 3)) if cfg.get("effects", False) else []
    peak = peaks[0] if peaks else None
    if peak is not None:
        T = f"{peak:.2f}"
        # punch-in zoom on every big moment (eases back out), shake on the biggest one
        z = "1" + "".join(f"+if(between(it,{q:.2f},{q:.2f}+0.7),0.25*(1-(it-{q:.2f})/0.7),0)" for q in peaks)
        shake = f"if(between(it,{T},{T}+0.4),18*sin(it*95),0)"
        vf += (
            f";{last_v}zoompan=z='{z}':d=1:s=1080x1920:fps=30"
            f":x='iw/2-(iw/zoom/2)+{shake}':y='ih/2-(ih/zoom/2)+{shake}'[vz];"
            f"[vz]drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill"
            f":enable='between(t,{T},{T}+0.08)'[vfx]"
        )
        last_v = "[vfx]"

    ass = make_captions(src, clip, cfg)
    if ass:
        vf += f";{last_v}ass='{ass}':fontsdir='{FONTS}'[vcap]"
        last_v = "[vcap]"

    hook_png = None
    if "_hook" not in clip:
        ai = ai_meme_caption(src, clip, cfg)
        if ai:
            clip["_hook"], clip["_title_text"] = ai
            try:
                hook_png, (pw, ph) = render_hook_png(ai[0], SHORTS / f"{clip['id']}_hook.png")
            except Exception as e:
                log(f"  (couldn't draw the AI caption: {e})")
                hook_png = None
    top = build_top_ass(clip, SHORTS / f"{clip['id']}_top.ass", split=clip.get("_split", False),
                        hook=not hook_png)
    vf += f";{last_v}ass='{top}':fontsdir='{FONTS}'[vtop]"
    last_v = "[vtop]"

    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(src)]
    audio_parts = ["[0:a]"]
    n_in = 1

    music = pick_music(src) if cfg.get("music", False) else None   # off by default: the streamer's voice is the content
    if music:
        vol = cfg.get("music_volume", 0.10)
        dur = track_duration(music) or 0
        offset = random.randint(0, max(0, int(dur) - 60))  # random start so shorts don't all sound the same
        cmd += ["-stream_loop", "-1", "-ss", str(offset), "-i", str(music)]
        # duck the music whenever the streamer talks (sidechain on the clip audio)
        vf += (
            f";[0:a]asplit=2[voice][sc]"
            f";[{n_in}:a]volume={vol}[mraw]"
            f";[mraw][sc]sidechaincompress=threshold=0.03:ratio=6:attack=10:release=350[m]"
        )
        audio_parts[0] = "[voice]"
        audio_parts.append("[m]")
        n_in += 1

    if hook_png:                               # the AI meme caption, on the face-cam/game line (or top)
        cmd += ["-loop", "1", "-i", str(hook_png)]
        hy = int(672 - ph / 2) if clip.get("_split") else 150
        vf += f";{last_v}[{n_in}:v]overlay=(W-w)/2:{hy}:shortest=1[vhook]"
        last_v = "[vhook]"
        n_in += 1

    for k, q in enumerate(peaks):              # a bass "boom" on each big moment
        ms = int(q * 1000)
        cmd += ["-f", "lavfi", "-i",
                "aevalsrc='0.9*sin(2*PI*55*t)*exp(-5*t)+0.35*(random(0)*2-1)*exp(-18*t)'"
                ":d=1:s=44100"]
        vf += f";[{n_in}:a]adelay={ms}|{ms},volume=1.4[boom{k}]"
        audio_parts.append(f"[boom{k}]")
        n_in += 1

    if len(audio_parts) > 1:
        vf += (
            f";{''.join(audio_parts)}amix=inputs={len(audio_parts)}"
            ":duration=first:dropout_transition=0:normalize=0[aout]"
        )
        maps = ["-map", last_v, "-map", "[aout]"]
    else:
        maps = ["-map", last_v, "-map", "0:a?"]

    cmd += [
        "-filter_complex", vf, *maps,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-shortest",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=900)
    finally:
        for f in (top, ass, hook_png):
            if f:
                Path(f).unlink(missing_ok=True)   # temp files, not needed after render
    return out


# ---------------- AI meme captions (free, GitHub Models) ----------------

AI_URL = "https://models.github.ai/inference/chat/completions"
AI_STYLE = """You write the on-screen caption for viral GTA 6 streamer clips on Instagram Reels / YouTube Shorts.
Style = meme clip pages: a short POV or relatable joke ABOUT what happens, not a plain description.
Examples of the vibe:
- "Pov: Bro After 22 Hours Non-Stop."
- "Pov: Gold Digger.. GTA 6"
- "They didn't let me press it..? 😭"
- "Speed when the cops pull up ☠️"
- "Wtf bruh what is this..? 😭"
Rules: 3-10 words, sentence case, 1-2 emojis from 😭 💀 ☠️ 😂 🔥 🌽 🤣 😳 at the end, no hashtags,
no quotes around it, nothing hateful, no slurs, swear words censored (f***). Use the streamer's name only
if it makes the joke better. Base it on what you SEE in the frames and what the streamer SAYS.
Reply with JSON only: {"hook": "...", "title": "..."} where title is a YouTube title (max 70 chars)
that starts with the hook and naturally includes the streamer name and "GTA 6"."""


def ai_meme_caption(src, clip, cfg):
    """Ask a free AI model (GitHub Models) for a POV-style meme caption. Returns (hook, title) or None."""
    import base64, tempfile
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not cfg.get("ai_captions", True):
        return None
    # 3 frames: before, at, and after the big moment
    rms = rms_profile(src)
    dur = len(rms) * 0.1 or 10
    peak = find_peak(src) or dur / 2
    images = []
    tmp = Path(tempfile.mkdtemp())
    try:
        for k, t in enumerate([max(0.2, peak - 3), peak + 0.3, min(dur - 0.2, peak + 3)]):
            jpg = tmp / f"f{k}.jpg"
            subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src),
                            "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(jpg)], timeout=60)
            if jpg.exists():
                images.append(base64.b64encode(jpg.read_bytes()).decode())
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    said = clip.get("_said", "").strip() or "(nothing clear / mostly silent)"
    info = (f"Streamer: {clip.get('broadcaster_name')}\nTwitch clip title: {clip.get('title', '')}\n"
            f"What the streamer says: {said[:600]}\nThe 3 frames are before / during / after the moment.")
    content = [{"type": "text", "text": info}] + [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}", "detail": "low"}} for b in images]
    body = {"model": cfg.get("ai_model", "openai/gpt-4o-mini"), "temperature": 0.9,
            "messages": [{"role": "system", "content": AI_STYLE}, {"role": "user", "content": content}]}
    try:
        r = requests.post(AI_URL, json=body, timeout=60, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
        hook = clean_word(str(data.get("hook", "")).strip().strip('"'))
        title = clean_word(str(data.get("title", "")).strip().strip('"')) or hook
        if not 2 <= len(hook.split()) <= 14:
            return None
        log(f"  AI caption: {hook}")
        return hook, title[:100]
    except Exception as e:
        log(f"  (AI caption failed: {str(e)[:120]}; using the normal hook)")
        return None


def _is_emoji(ch):
    o = ord(ch)
    return o >= 0x2600 and not (0x3000 <= o <= 0x30FF) or o in (0x200D, 0xFE0F)


def render_hook_png(text, out_path, width=1000):
    """Draw the hook like meme pages do: white text, thick black outline, real colour emojis."""
    from PIL import Image, ImageDraw, ImageFont
    size = 78
    font = ImageFont.truetype(str(FONTS / "Anton-Regular.ttf"), size)
    emoji_font = None
    for p in ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", "/usr/share/fonts/noto/NotoColorEmoji.ttf",
              "/System/Library/Fonts/Apple Color Emoji.ttc"]:
        if Path(p).exists():
            try:
                emoji_font = ImageFont.truetype(p, 109)
                break
            except Exception:
                pass
    # split into words; each word = list of (is_emoji, text) pieces
    words = []
    for w in text.split():
        pieces, cur, cur_e = [], "", None
        for ch in w:
            if ch in ("\ufe0f", "\u200d"):
                continue
            e = _is_emoji(ch)
            if cur and e != cur_e:
                pieces.append((cur_e, cur))
                cur = ""
            cur, cur_e = cur + ch, e
        if cur:
            pieces.append((cur_e, cur))
        words.append(pieces)
    space = font.getlength(" ")
    def piece_w(e, t):
        return size * 1.05 * len(t) if e else font.getlength(t)
    word_w = lambda pcs: sum(piece_w(e, t) for e, t in pcs)
    lines, cur, cur_w = [], [], 0
    for pcs in words:
        w = word_w(pcs)
        if cur and cur_w + space + w > width - 40:
            lines.append(cur)
            cur, cur_w = [], 0
        cur.append(pcs)
        cur_w += (space if cur_w else 0) + w
    if cur:
        lines.append(cur)
    line_h = int(size * 1.25)
    img = Image.new("RGBA", (width, line_h * len(lines) + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 10
    for ln in lines:
        total = sum(word_w(p) for p in ln) + space * (len(ln) - 1)
        x = (width - total) / 2
        for pcs in ln:
            for e, t in pcs:
                if e and emoji_font:
                    for ch in t:
                        g = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
                        ImageDraw.Draw(g).text((0, 0), ch, font=emoji_font, embedded_color=True)
                        g = g.crop(g.getbbox() or (0, 0, 1, 1)).resize((int(size * 0.95), int(size * 0.95)))
                        img.alpha_composite(g, (int(x), int(y + size * 0.12)))
                        x += size * 1.05
                elif not e:
                    d.text((x, y), t, font=font, fill="white", stroke_width=7, stroke_fill="black")
                    x += font.getlength(t)
            x += space
        y += line_h
    img.save(out_path)
    return out_path, img.size


# ---------------- YouTube ----------------

def youtube_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_file = BASE / "yt_token.json"
    if not token_file.exists() and os.environ.get("YT_TOKEN_JSON"):
        token_file.write_text(os.environ["YT_TOKEN_JSON"])   # from GitHub Secrets
    if not token_file.exists():
        sys.exit("yt_token.json missing — run: python3 auth_youtube.py")
    creds = Credentials.from_authorized_user_file(
        str(token_file), ["https://www.googleapis.com/auth/youtube.upload"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


TITLE_EMOJIS = ["💀", "😭", "🔥", "😂", "😳", "🤣", "😱", "", ""]   # "" = sometimes no emoji

KEYWORD_LINES = [
    "{streamer} playing {game} on stream",
    "{streamer} {game} funny moment",
    "{streamer} live on Twitch, {game}",
    "Best {game} moment from {streamer}'s stream",
]

COMMENT_HOOKS = [
    "Would you have survived this? 😂",
    "Rate this moment 1-10 👇",
    "Tag a friend who plays like this 💀",
    "What would you have done here? 👀",
    "Who's the funniest {game} streamer so far? 👇",
    "Funniest part? Comment the timestamp 😭",
]

CREDIT_LINES = [
    "All credit to the streamer. Want a clip taken down? Comment and it'll be removed.",
    "Clip belongs to the streamer. Ask in the comments and it'll be taken down.",
    "Credit to the creator. Removal requests in the comments are honored.",
]


def game_labels(cfg):
    """('GTA 6', '#gta6') from the configured Twitch category."""
    name = cfg.get("game_name", "")
    if name.endswith(" VI"):
        return "GTA 6", "#gta6"
    if name.endswith(" V"):
        return "GTA 5", "#gta5"
    short = re.sub(r"[^A-Za-z0-9]", "", name)
    return name, "#" + short.lower() if short else "#gaming"


# how people actually type these names when searching (Twitch display names have no spaces)
NICE_NAMES = {
    "kaicenat": "Kai Cenat", "caseoh_": "CaseOh", "stableronaldo": "Stable Ronaldo",
    "plaqueboymax": "Plaqueboymax", "yourragegaming": "YourRAGE", "agent00": "Agent 00",
    "moistcr1tikal": "penguinz0", "loltyler1": "Tyler1", "hasanabi": "HasanAbi",
}


def post_text(clip, cfg):
    """Shared pieces for YouTube + Instagram: title, keyword line, comment hook, tags."""
    raw_name = clip["broadcaster_name"]
    handle = re.sub(r"[^A-Za-z0-9_]", "", raw_name).lower()
    names = {**NICE_NAMES, **{k.lower(): v for k, v in cfg.get("streamer_names", {}).items()}}
    streamer = names.get(handle, raw_name)
    game, game_tag = game_labels(cfg)
    _, text = pick_hook(clip)
    if streamer != raw_name:      # "KaiCenat was..." -> "Kai Cenat was..."
        text = re.sub(rf"\b{re.escape(raw_name.rstrip('_'))}_?\b", streamer, text, flags=re.IGNORECASE)
    emoji = "" if any(_is_emoji(ch) for ch in text) else random.choice(TITLE_EMOJIS)

    # searchable suffix: "| Kai Cenat GTA 6", without repeating what the hook already says
    has_name = streamer.lower() in text.lower() or raw_name.lower() in text.lower()
    has_game = "gta" in text.lower()
    suffix = " ".join(x for x in ([] if has_name else [streamer]) + ([] if has_game else [game]))
    title = f"{text} {emoji}".strip() + (f" | {suffix}" if suffix else "")
    if len(title) > 100:
        title = title[:97].rsplit(" ", 1)[0] + "..."

    return {
        "streamer": streamer, "handle": handle, "game": game, "game_tag": game_tag,
        "title": title, "text": text, "emoji": emoji,
        "keyword": random.choice(KEYWORD_LINES).format(streamer=streamer, game=game),
        "comment": random.choice(COMMENT_HOOKS).format(game=game),
        "url": f"https://twitch.tv/{handle}" if handle else "https://twitch.tv",
        "clips_tag": game_tag + "clips",            # e.g. #gta6clips
    }


def upload_short(yt, video_path, clip, cfg):
    from googleapiclient.http import MediaFileUpload

    t = post_text(clip, cfg)
    # first 3 hashtags show above the title on YouTube, so most important first
    tags = [t["game_tag"]] + ([f"#{t['handle']}"] if t["handle"] else []) + [t["clips_tag"], "#shorts"]
    tags.append(random.choice(["#gaming", "#twitch", "#funnymoments", "#gta"]))
    description = (
        f"{t['keyword']}\n{t['comment']}\n\n"
        f"🎥 Streamer: {t['streamer']} | {t['url']}\n"
        f"New {t['game']} clips every day.\n\n"
        f"{' '.join(tags)}\n\n{random.choice(CREDIT_LINES)}"
    )
    g, st = t["game"].lower(), t["streamer"].lower()
    search_tags = [f"{st} {g}", f"{g} funny moments", f"{g} clips", f"{g} gameplay",
                   g, st, "twitch clips", "gta funny moments"]

    body = {
        "snippet": {
            "title": t["title"],
            "description": description,
            "tags": search_tags,
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": cfg.get("privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ---------------- Instagram Reels ----------------

IG_API = "https://graph.facebook.com/v27.0"
IG_UPLOAD = "https://rupload.facebook.com/ig-api-upload/v27.0"


SEXY_WORDS = ["strip club", "stripper", "strip", "lap dance", "lapdance", "naked", "nude", "sex", "gyatt",
              "hooker", "boob", "booty", "twerk", "onlyfans", "porn", "por*", "nsfw", "thirst", "brothel",
              "escort", "hoe", "freak", "lust", "gold digger", "bedroom"]


def instagram_safe(clip, cfg):
    """Instagram strikes accounts for nudity AND sexual scenes even when blurred, so anything
    with nudity or a sexual-sounding title/speech skips Instagram (it still goes to YouTube)."""
    if clip.get("_nude", 0) > cfg.get("ig_nudity_limit", 0.0):
        return False, "nudity in the clip"
    text = (clip.get("title", "") + " " + clip.get("_said", "") + " " + clip.get("_title_text", "")).lower()
    hit = next((w for w in SEXY_WORDS if re.search(rf"\b{re.escape(w)}", text)), None)
    if hit:
        return False, f'sexual word "{hit}"'
    return True, ""


def instagram_enabled(cfg):
    return cfg.get("instagram", True) and os.environ.get("IG_USER_ID") and os.environ.get("IG_ACCESS_TOKEN")


def post_reel(video_path, caption):
    """Upload a local video as an Instagram Reel (Facebook Login resumable upload). Returns media id."""
    import time
    ig_id, token = os.environ["IG_USER_ID"], os.environ["IG_ACCESS_TOKEN"]
    auth = {"Authorization": f"OAuth {token}"}

    # 1. create an upload container
    r = requests.post(f"{IG_API}/{ig_id}/media", headers=auth, timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": caption[:2200], "share_to_feed": "true",
    })
    r.raise_for_status()
    container = r.json()["id"]

    # 2. send the video file
    data = Path(video_path).read_bytes()
    r = requests.post(f"{IG_UPLOAD}/{container}", timeout=600, data=data,
                      headers={**auth, "offset": "0", "file_size": str(len(data))})
    r.raise_for_status()

    # 3. wait for Instagram to finish processing
    for _ in range(60):                       # up to ~10 minutes
        st = requests.get(f"{IG_API}/{container}", headers=auth, timeout=30,
                          params={"fields": "status_code,status"}).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing failed: {st.get('status')}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")

    # 4. publish
    r = requests.post(f"{IG_API}/{ig_id}/media_publish", headers=auth, timeout=60,
                      data={"creation_id": container})
    r.raise_for_status()
    return r.json()["id"]


def reel_caption(clip, cfg):
    t = post_text(clip, cfg)
    me = cfg.get("channel_handle", "@clipnidia")
    tags = " ".join([t["game_tag"], t["clips_tag"]] + ([f"#{t['handle']}"] if t["handle"] else []) + ["#gaming", "#reels"])
    return (
        f"{t['text']} {t['emoji']}".strip() + "\n"
        f"{t['keyword']} 🎮\n\n"
        f"{t['comment']}\n"
        f"Follow {me} for daily {t['game']} clips\n\n"
        f"🎥 twitch.tv/{t['handle']}\n"
        f"All credit to the streamer. DM for removal.\n\n{tags}"
    )


# ---------------- Main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build shorts but don't upload")
    args = ap.parse_args()

    cfg = load_config()
    history = load_history()
    DOWNLOADS.mkdir(exist_ok=True)
    SHORTS.mkdir(exist_ok=True)

    log("=== run started ===")
    if not args.dry_run:
        now = datetime.now(timezone.utc)
        until = history.get("yt_cooldown_until")
        if until and datetime.fromisoformat(until) > now:
            log(f"YouTube's daily upload limit was hit - pausing until {until[:16]} UTC (saves GitHub minutes)")
            return
        recent = [t for t in history.get("upload_times", [])
                  if now - datetime.fromisoformat(t) < timedelta(hours=24)]
        cap = cfg.get("max_uploads_per_day", 10)
        if len(recent) >= cap:
            log(f"already posted {len(recent)} shorts in the last 24h (limit {cap}) - skipping this run")
            return
    token = twitch_token(cfg)
    clips = fetch_clips(cfg, token)
    done = set(history["posted_clip_ids"]) | set(history.get("skipped_clip_ids", []))
    fallback = cfg.get("fallback_lookback_hours", 12)
    if not [c for c in clips if c["id"] not in done] and fallback > cfg.get("lookback_hours", 6):
        log(f"nothing fresh: looking back {fallback}h instead")
        clips = fetch_clips({**cfg, "lookback_hours": fallback}, token)
    log(f"found {len(clips)} candidate clips")

    done = set(history["posted_clip_ids"]) | set(history.get("skipped_clip_ids", []))
    new_clips = [c for c in clips if c["id"] not in done]
    if len(new_clips) < len(clips):
        log(f"  skipping {len(clips) - len(new_clips)} already posted / skipped")
    max_uploads = cfg.get("max_uploads_per_run", 2)
    per_streamer = cfg.get("max_per_streamer", 1)

    # 1) shortlist from the top of the hierarchy (a couple per streamer so we can choose their best)
    shortlist, count = [], {}
    for c in new_clips:
        bid = c.get("broadcaster_id")
        if count.get(bid, 0) >= 2:
            continue
        shortlist.append(c)
        count[bid] = count.get(bid, 0) + 1
        if len(shortlist) >= cfg.get("candidates_per_run", 4):
            break
    if not shortlist:
        log("nothing new to post")
        return

    # 2) score each one: funny / achievement / boring
    scored = []
    for c in shortlist:
        try:
            raw = download_clip(c, cfg)
            mode = cfg.get("nudity", "blur")
            nude = nudity_fraction(raw, cfg) if mode in ("blur", "skip") else 0.0
            c["_nude"] = nude
            too_much = nude > 0.05 if mode == "skip" else nude >= cfg.get("nudity_skip_fraction", 1.01)
            if too_much:                                  # mostly nude (strip club etc.) = skip; a quick moment gets blurred later
                log(f"  skip (nudity in {nude:.0%} of clip) {c['broadcaster_name']} - {c['title'][:50]}")
                history.setdefault("skipped_clip_ids", []).append(c["id"])
                save_history(history)
                raw.unlink(missing_ok=True)
                continue
            c["_score"], c["_category"] = score_moment(raw, c, cfg)
            cam = "cam" if c.get("_cam") else "no cam"
            log(f"  score {c['_score']:>5} [{c['_category']}, {cam}] {c['broadcaster_name']} - {c['title'][:50]}")
            log(f"        ({c.get('_why', '')})")
            scored.append(c)
        except Exception as e:
            log(f"  skip {c['id']}: {e}")

    # 3) best moments first; boring ones only if nothing else
    scored.sort(key=lambda c: (c["_category"] == "boring", -c["_score"]))
    picked, used = [], {}
    for c in scored:
        bid = c.get("broadcaster_id")
        if used.get(bid, 0) >= per_streamer:
            continue
        if c["_category"] == "boring" and not cfg.get("allow_boring", False) and picked:
            continue
        picked.append(c)
        used[bid] = used.get(bid, 0) + 1
        if len(picked) >= max_uploads:
            break
    for c in scored:                               # delete downloads we didn't pick
        if c not in picked:
            (DOWNLOADS / f"{c['id']}.mp4").unlink(missing_ok=True)
    if not picked:
        log("nothing worth posting this run")
        return

    yt = None if args.dry_run else youtube_service()

    for clip in picked:
        try:
            log(f"processing: {clip['broadcaster_name']} — {clip['title']} ({clip['view_count']} views)")
            raw = download_clip(clip, cfg)
            tight = tighten_clip(raw, clip, cfg)
            clean = tight
            if cfg.get("nudity", "blur") == "blur":
                try:
                    clean = censor_clip(tight, clip, cfg)
                except ImportError:
                    pass                                  # tool missing: post as-is
            short = make_vertical(clean, clip, cfg)
            if args.dry_run:
                log(f"dry run — short built at {short}")
            else:
                try:
                    vid = upload_short(yt, short, clip, cfg)
                except Exception as e:
                    if "uploadLimitExceeded" in str(e) or "exceeded the number of videos" in str(e):
                        pause = cfg.get("limit_pause_hours", 12)
                        history["yt_cooldown_until"] = (datetime.now(timezone.utc) + timedelta(hours=pause)).isoformat()
                        save_history(history)
                        log(f"YouTube says the channel hit its daily upload limit - pausing uploads for {pause}h")
                        break
                    raise
                log(f"uploaded: https://youtube.com/shorts/{vid}")
                history["upload_times"] = (history.get("upload_times", []) + [datetime.now(timezone.utc).isoformat()])[-60:]
                ok_ig, why = instagram_safe(clip, cfg)
                if instagram_enabled(cfg) and not ok_ig:
                    log(f"instagram: skipped this one ({why}) - Instagram strikes sexual content even blurred")
                elif instagram_enabled(cfg):
                    try:
                        rid = post_reel(short, reel_caption(clip, cfg))
                        log(f"instagram reel posted: {rid}")
                    except Exception as e:      # Instagram trouble never blocks YouTube
                        detail = getattr(getattr(e, "response", None), "text", "")[:300]
                        log(f"instagram failed: {e} {detail}")
                # only real uploads count as posted — test runs can reuse clips
                history["posted_clip_ids"].append(clip["id"])
                save_history(history)
            raw.unlink(missing_ok=True)  # keep disk clean, keep the short
            for f in {tight, clean} - {raw}:
                Path(f).unlink(missing_ok=True)
        except Exception as e:
            log(f"ERROR on clip {clip['id']}: {e}")

    log("=== run finished ===")


if __name__ == "__main__":
    main()
