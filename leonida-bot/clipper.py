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
    if broadcasters:
        users = twitch_get(cfg, token, "users", [("login", b.lower()) for b in broadcasters])
        for u in users:
            clips += twitch_get(
                cfg, token, "clips",
                {"broadcaster_id": u["id"], "started_at": started_at, "first": 20},
            )
    if game_id:
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
    max_len = cfg.get("max_clip_seconds", 30)
    unique = [c for c in unique if 5 <= c.get("duration", 0) <= max_len]
    log(f"  after length (5-{max_len}s): {len(unique)}")
    lang = cfg.get("language", "en")
    if lang:
        unique = [c for c in unique if c.get("language", "").startswith(lang)]
        log(f"  after language ({lang}): {len(unique)}")
    unique.sort(key=lambda c: c["view_count"], reverse=True)
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
for seg in segs:
    for w in seg.words or []:
        t = w.word.strip()
        if t:
            out.append([t, w.start, w.end])
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


def find_peak(src):
    """Time (s) of the loudest moment, or None if the clip has no clear spike."""
    import array, math
    try:
        r = subprocess.run(
            [ffmpeg_path(), "-v", "error", "-i", str(src), "-ac", "1", "-ar", "8000",
             "-f", "s16le", "-"],
            capture_output=True, timeout=120,
        )
        samples = array.array("h", r.stdout)
    except Exception:
        return None
    win = 800  # 0.1 s windows
    rms = []
    for i in range(0, len(samples) - win, win):
        chunk = samples[i:i + win]
        rms.append(math.sqrt(sum(x * x for x in chunk) / win))
    if len(rms) < 10:
        return None
    mean = sum(rms) / len(rms)
    peak_i = max(range(len(rms)), key=lambda i: rms[i])
    if mean == 0 or rms[peak_i] / mean < 1.8:
        return None  # no real spike, effects would look random
    return max(0.0, peak_i * 0.1 - 0.05)


GENERIC_TITLES = {"", "lol", "lmao", "w", "l", "clip", "omg", "bruh", "wtf", "xd", "kek",
                  "pog", "poggers", "lul", "omegalul", "kekw", "haha", "wow", "nice", "gg"}

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
        t = random.choice(HOOK_TEMPLATES)
        hook = t.format(name=display.upper())
        text = t.format(name="{N}").lower()
        text = (text[0].upper() + text[1:]).replace("{n}", display)
    clip["_hook"], clip["_title_text"] = hook, text
    return hook, text


def make_hook(clip):
    return pick_hook(clip)[0]


def build_top_ass(clip, out_path):
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
    lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Hook,{pop}{hook}")
    out_path.write_text(header + "\n".join(lines))
    return out_path


def make_vertical(src, clip, cfg):
    """16:9 clip -> 1080x1920 short: blur bg, peak zoom/shake/flash/boom, captions, banner, music."""
    import re
    out = SHORTS / f"{clip['id']}_short.mp4"

    vf = (
        "[0:v]scale=-2:1920,crop=1080:1920,boxblur=24:6,eq=brightness=-0.08[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30[v1]"
    )
    last_v = "[v1]"

    peak = find_peak(src) if cfg.get("effects", True) else None
    if peak is not None:
        T = f"{peak:.2f}"
        # punch-in zoom that eases back out + shake
        z = f"if(between(it,{T},{T}+0.7),1+0.25*(1-(it-{T})/0.7),1)"
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

    top = build_top_ass(clip, SHORTS / f"{clip['id']}_top.ass")
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

    if peak is not None:
        ms = int(peak * 1000)
        cmd += ["-f", "lavfi", "-i",
                "aevalsrc='0.9*sin(2*PI*55*t)*exp(-5*t)+0.35*(random(0)*2-1)*exp(-18*t)'"
                ":d=1:s=44100"]
        vf += f";[{n_in}:a]adelay={ms}|{ms},volume=1.4[boom]"
        audio_parts.append("[boom]")
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

DESCRIPTION_STYLES = [
    "{title}\n\nFull credit to {streamer}, catch the stream at {url}\n\n{tags}",
    "{streamer} on stream 🎮\nWatch live: {url}\n\n{tags}",
    "Clipped from {streamer}'s stream ({url}).\nFollow for more {game} moments every day.\n\n{tags}",
    "{title}\n\nStreamer: {streamer} | {url}\nNew {game} clips daily.\n\n{tags}",
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


def upload_short(yt, video_path, clip, cfg):
    from googleapiclient.http import MediaFileUpload

    streamer = clip["broadcaster_name"]
    handle = re.sub(r"[^A-Za-z0-9_]", "", streamer).lower()
    url = f"https://twitch.tv/{handle}" if handle else "https://twitch.tv"
    game, game_tag = game_labels(cfg)
    _, title_text = pick_hook(clip)

    emoji = random.choice(TITLE_EMOJIS)
    title = f"{title_text} {emoji}".strip()
    if len(title) > 100:
        title = title[:97].rsplit(" ", 1)[0] + "..."

    extra = random.sample(["#gta", "#shorts", "#gaming", "#twitch", "#funny"], 2)
    tag_list = [game_tag] + ([f"#{handle}"] if handle else []) + extra   # 3-4 tags, game always correct
    description = random.choice(DESCRIPTION_STYLES).format(
        title=title_text, streamer=streamer, url=url, game=game, tags=" ".join(tag_list)
    ) + "\n\n" + random.choice(CREDIT_LINES)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [game.lower(), game_tag[1:], "grand theft auto", streamer, "twitch clips", "shorts"],
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
    streamer = clip["broadcaster_name"]
    handle = re.sub(r"[^A-Za-z0-9_]", "", streamer).lower()
    game, game_tag = game_labels(cfg)
    _, title_text = pick_hook(clip)
    emoji = random.choice(TITLE_EMOJIS)
    tags = " ".join([game_tag, "#gta", f"#{handle}" if handle else "#twitch", "#gaming", "#reels"])
    return (f"{title_text} {emoji}".strip()
            + f"\n\n🎥 {streamer} on Twitch (twitch.tv/{handle})\nFollow for daily {game} clips\n\n{tags}"
            + "\n\nAll credit to the streamer. DM for removal.")


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
    picked = new_clips[:max_uploads]
    if not picked:
        log("nothing new to post")
        return

    yt = None if args.dry_run else youtube_service()

    for clip in picked:
        try:
            log(f"processing: {clip['broadcaster_name']} — {clip['title']} ({clip['view_count']} views)")
            raw = download_clip(clip)
            short = make_vertical(raw, clip, cfg)
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
        except Exception as e:
            log(f"ERROR on clip {clip['id']}: {e}")

    log("=== run finished ===")


if __name__ == "__main__":
    main()
