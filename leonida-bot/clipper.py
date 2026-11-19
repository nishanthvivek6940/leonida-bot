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
    unique = [c for c in unique if c["view_count"] >= cfg.get("min_views", 1000)]
    log(f"  after min views ({cfg.get('min_views', 1000)}+): {len(unique)}")
    # big names first: streamers on your list always count, anyone else only if the clip went viral
    viral = cfg.get("category_min_views", 20000)
    unique = [c for c in unique if c.get("broadcaster_id") in list_ids or c["view_count"] >= viral]
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
        return (tier, rank.get(bid, 999), -c["view_count"])
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


def download_clip(clip):
    out = DOWNLOADS / f"{clip['id']}.mp4"
    if out.exists():
        return out
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-o", str(out), clip["url"]]
    subprocess.run(cmd, check=True, timeout=300)
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
                txt = cw.upper().replace("{", "").replace("}", "")
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


def make_captions(src, clip, cfg):
    """Returns path to .ass subtitle file, or None."""
    if not cfg.get("captions", True):
        return None
    try:
        words = transcribe_words(src, cfg.get("caption_timeout", 240))
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


def find_peak(src):
    """Time (s) of the loudest moment, or None if the clip has no clear spike."""
    rms = rms_profile(src)
    if len(rms) < 10:
        return None
    mean = sum(rms) / len(rms)
    peak_i = max(range(len(rms)), key=lambda i: rms[i])
    if mean == 0 or rms[peak_i] / mean < 1.8:
        return None  # no real spike, effects would look random
    return max(0.0, peak_i * 0.1 - 0.05)


def plan_cuts(rms, peak, cfg):
    """Which parts of the clip to keep: short build-up, the payoff, the reaction; no dead air."""
    dur = len(rms) * 0.1
    pre = cfg.get("max_buildup_seconds", 10)
    post = cfg.get("after_payoff_seconds", 4)
    start, end = max(0.0, peak - pre), min(dur, peak + post)

    # dead air = quiet stretches before the payoff (quiet = under 35% of the clip's average loudness)
    mean = sum(rms) / len(rms)
    quiet = [v < mean * 0.35 for v in rms]
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
    """Jump-cut the clip so the payoff comes fast. Returns the new file (or the original)."""
    if not cfg.get("jump_cuts", True):
        return src
    rms = rms_profile(src)
    peak = find_peak(src)
    if peak is None or len(rms) < 20:
        return src                                    # no clear payoff: leave the clip alone
    keep = plan_cuts(rms, peak, cfg)
    dur = len(rms) * 0.1
    if len(keep) == 1 and keep[0][0] < 0.3 and keep[0][1] > dur - 0.3:
        return src                                    # nothing worth cutting
    expr = "+".join(f"between(t,{a},{b})" for a, b in keep)
    out = DOWNLOADS / f"{clip['id']}_cut.mp4"
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(src),
           "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
           "-af", f"aselect='{expr}',asetpts=N/SR/TB",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(out)]
    subprocess.run(cmd, check=True, timeout=600)
    new_len = sum(b - a for a, b in keep)
    log(f"  jump cuts: {dur:.0f}s -> {new_len:.0f}s ({len(keep)} pieces)")
    return out


# ---------------- Is this clip actually good? ----------------

FUNNY_WORDS = ["haha", "hahaha", "lmao", "lol", "bro", "bruh", "nah", "why", "wtf", "yo",
               "what", "dude", "stop", "help", "noo", "dead", "crying", "chat", "dawg"]
HYPE_WORDS = ["let's go", "lets go", "no way", "oh my god", "omg", "clutch", "insane", "gg",
              "we did it", "finally", "first try", "yes", "easy", "cracked", "goated", "let's gooo"]
TITLE_FUNNY = ["lol", "lmao", "funny", "fail", "dies", "died", "crash", "💀", "😭", "bro", "why", "wtf", "rip"]
TITLE_HYPE = ["insane", "clutch", "first try", "no way", "crazy", "best", "perfect", "world record", "god"]


def score_moment(raw, clip, cfg):
    """Free heuristic: how funny / hype is this clip? Returns (score, category)."""
    import math
    text = ""
    try:
        words = transcribe_words(raw, cfg.get("caption_timeout", 240))
        text = " " + " ".join(w for w, _, _ in words).lower() + " "
    except Exception as e:
        log(f"  (scoring without speech: {e})")
    funny = sum(text.count(f" {w}") for w in FUNNY_WORDS)
    hype = sum(text.count(f" {w}") for w in HYPE_WORDS)
    title = clip.get("title", "").lower()
    funny += 2 * sum(k in title for k in TITLE_FUNNY)
    hype += 2 * sum(k in title for k in TITLE_HYPE)

    rms = rms_profile(raw)
    excite = 0.0
    if len(rms) >= 10:
        mean = sum(rms) / len(rms) or 1
        excite = max(0.0, max(rms) / mean - 1.5)              # big spike = yelling/screaming
        excite += sum(1 for v in rms if v > mean * 2.2) / 10   # how long it stays loud

    moment = 1.5 * min(funny, 8) + 1.5 * min(hype, 8) + 2 * min(excite, 4)
    if funny == 0 and hype == 0 and excite < 0.5:
        category = "boring"
    elif hype > funny:
        category = "achievement"
    else:
        category = "funny"
    ladder = {0: 4, 1: 1.5}.get(clip.get("_tier", 2), 0)        # ranked + live streamers still get a head start
    ladder += max(0, 2 - clip.get("_rank", 999) * 0.1)          # higher on your list = small bonus
    score = moment + ladder + math.log10(max(clip["view_count"], 1))
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
    else:
        pool = ACHIEVEMENT_HOOKS if clip.get("_category") == "achievement" else HOOK_TEMPLATES
        t = random.choice(pool)
        hook = t.format(name=display.upper())
        text = t.format(name="{N}").lower()
        text = (text[0].upper() + text[1:]).replace("{n}", display)
    clip["_hook"], clip["_title_text"] = hook, text
    return hook, text


def make_hook(clip):
    return pick_hook(clip)[0]


def build_top_ass(clip, out_path, split=False):
    """Streamer name (small) + hook line (big, wraps to 2 lines) in the top blurred area."""
    import re
    def esc(t):
        return t.replace("\\", "").replace("{", "(").replace("}", ")")
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


def find_facecam(src):
    """Never let face detection kill a clip: on any error, fall back to the blur layout."""
    try:
        return _find_facecam(src)
    except Exception as e:
        log(f"  (face-cam check failed: {type(e).__name__}: {e})")
        return None


def _find_facecam(src):
    """Find the streamer's webcam: a face that stays in the same spot across the clip.
    Returns (x, y, w, h, W, H) crop box in source pixels, or None."""
    try:
        import cv2
    except Exception:
        return None
    cap = cv2.VideoCapture(str(src))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    W, H = cap.get(3), cap.get(4)
    if n < 10 or not W or not H:
        cap.release()
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    dets = []
    for k in range(8):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (k + 0.5) / 8))
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = casc.detectMultiScale(gray, 1.1, 6, minSize=(28, 28))
        dets.append([(x * 2, y * 2, w * 2, h * 2) for (x, y, w, h) in faces])
    cap.release()
    if not dets:
        return None
    best = None
    for f in [f for fs in dets for f in fs]:
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        hits = []
        for fs in dets:
            m = [g for g in fs if abs(g[0] + g[2] / 2 - cx) < W * 0.05 and abs(g[1] + g[3] / 2 - cy) < H * 0.07]
            if m:
                hits.append(m[0])
        if len(hits) >= max(3, int(0.6 * len(dets))) and (best is None or len(hits) > len(best)):
            best = hits
    if not best:
        return None                                   # no steady face = no webcam (or face hidden)
    med = lambda i: sorted(h[i] for h in best)[len(best) // 2]
    fx, fy, fw, fh = med(0), med(1), med(2), med(3)
    if fw < H * 0.05:
        return None                                   # too tiny to look good blown up
    cw = min(W, fw * 3.0)
    ch = cw * 672 / 1080
    if ch > H:
        ch, cw = H, H * 1080 / 672
    cx, cy = fx + fw / 2, fy + fh / 2 + fh * 0.25
    x = min(max(0, cx - cw / 2), W - cw)
    y = min(max(0, cy - ch / 2), H - ch)
    ev = lambda v: int(v) // 2 * 2
    return ev(x), ev(y), ev(cw), ev(ch), ev(W), ev(H)


def make_vertical(src, clip, cfg):
    """16:9 clip -> 1080x1920 short: blur bg, peak zoom/shake/flash/boom, captions, banner, music."""
    import re
    out = SHORTS / f"{clip['id']}_short.mp4"

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

    peaks = find_peaks(src, cfg.get("max_booms", 3)) if cfg.get("effects", True) else []
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

    top = build_top_ass(clip, SHORTS / f"{clip['id']}_top.ass", split=clip.get("_split", False))
    vf += f";{last_v}ass='{top}':fontsdir='{FONTS}'[vtop]"
    last_v = "[vtop]"

    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(src)]
    audio_parts = ["[0:a]"]
    n_in = 1

    music = pick_music(src)
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
        for f in (top, ass):
            if f:
                Path(f).unlink(missing_ok=True)   # temp text files, not needed after render
    return out


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
    emoji = random.choice(TITLE_EMOJIS)

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
    token = twitch_token(cfg)
    clips = fetch_clips(cfg, token)
    log(f"found {len(clips)} candidate clips")

    new_clips = [c for c in clips if c["id"] not in history["posted_clip_ids"]]
    if len(new_clips) < len(clips):
        log(f"  skipping {len(clips) - len(new_clips)} already posted")
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
            raw = download_clip(c)
            c["_score"], c["_category"] = score_moment(raw, c, cfg)
            log(f"  score {c['_score']:>5} [{c['_category']}] {c['broadcaster_name']} - {c['title'][:50]}")
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
            raw = download_clip(clip)
            tight = tighten_clip(raw, clip, cfg)
            short = make_vertical(tight, clip, cfg)
            if args.dry_run:
                log(f"dry run — short built at {short}")
            else:
                vid = upload_short(yt, short, clip, cfg)
                log(f"uploaded: https://youtube.com/shorts/{vid}")
                if instagram_enabled(cfg):
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
            if tight != raw:
                Path(tight).unlink(missing_ok=True)
        except Exception as e:
            log(f"ERROR on clip {clip['id']}: {e}")

    log("=== run finished ===")


if __name__ == "__main__":
    main()
