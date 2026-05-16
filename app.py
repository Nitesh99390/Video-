"""
app.py — VideoForge Cloud (Render.com Free-Tier Optimised)
===========================================================
Architecture fixes applied:
  ✓ Audio stream copy (-c:a copy) — zero re-encode, minimal CPU/RAM
  ✓ Non-blocking progress parsing via readline() + -loglevel warning
  ✓ Aggressive garbage collection: src files deleted immediately in finally,
    output MP4 purged after the /download response is streamed
  ✓ Background reaper thread cleans jobs older than 1 hour
  ✓ All FFmpeg video flags kept: ultrafast · stillimage · yuv420p · faststart
  ✓ Single-file deploy: Flask backend + embedded glassmorphic Emerald frontend
"""

import os
import threading
import time
import uuid
import json
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string, send_file

# ═══════════════════════════════════════════════════════════════════
#  APP SETUP
# ═══════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024   # 150 MB hard cap

TEMP_DIR = Path("/tmp/vf_jobs")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job registry   { job_id: dict }
jobs: dict[str, dict] = {}

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

QUALITY_PRESETS = {
    "fast":     {"preset": "ultrafast", "crf": "28"},
    "balanced": {"preset": "fast",      "crf": "23"},
    "hq":       {"preset": "medium",    "crf": "18"},
}

# ═══════════════════════════════════════════════════════════════════
#  FFPROBE HELPER
# ═══════════════════════════════════════════════════════════════════
def get_audio_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe, or 0 on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        fmt = json.loads(result.stdout).get("format", {})
        return float(fmt.get("duration", 0))
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════
#  BACKGROUND REAPER  (cleans stale jobs every 30 minutes)
# ═══════════════════════════════════════════════════════════════════
def _reaper():
    """Delete job directories and registry entries older than 1 hour."""
    while True:
        time.sleep(1800)                            # run every 30 min
        cutoff = time.time() - 3600                 # 1-hour TTL
        stale  = [jid for jid, j in list(jobs.items()) if j.get("created", 0) < cutoff]
        for jid in stale:
            job = jobs.pop(jid, {})
            out = job.get("output_path")
            if out:
                try:
                    Path(out).unlink(missing_ok=True)
                except OSError:
                    pass
            job_dir = TEMP_DIR / jid
            try:
                for f in job_dir.iterdir():
                    f.unlink(missing_ok=True)
                job_dir.rmdir()
            except OSError:
                pass

threading.Thread(target=_reaper, daemon=True, name="reaper").start()


# ═══════════════════════════════════════════════════════════════════
#  RENDER WORKER  (background thread per job)
# ═══════════════════════════════════════════════════════════════════
def render_worker(
    job_id:   str,
    img_path: Path,
    aud_path: Path,
    out_path: Path,
    quality:  str,
) -> None:
    """
    Runs FFmpeg with:
      • -c:a copy        → zero audio re-encode (critical for 512 MB RAM)
      • -loglevel warning → stderr warnings visible; stdout kept for -progress
      • readline()       → non-blocking, line-by-line progress parsing; no deadlock
    Cleans up source files immediately in finally regardless of outcome.
    """
    job = jobs[job_id]

    duration         = get_audio_duration(aud_path)
    job["duration"]  = duration
    job["status"]    = "rendering"
    job["start_time"] = time.time()

    cfg = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])

    command = [
        "ffmpeg",
        # Input: static image looped at 1 fps (minimal decode cost)
        "-loop", "1", "-framerate", "1", "-i", str(img_path),
        # Input: audio (stream-copied — no decode/re-encode)
        "-i", str(aud_path),
        # Video codec
        "-c:v", "libx264",
        "-preset", cfg["preset"],
        "-tune", "stillimage",
        "-crf", cfg["crf"],
        "-pix_fmt", "yuv420p",
        # ─── KEY OPTIMISATION: copy audio stream, skip re-encode ───
        "-c:a", "copy",
        # Web-optimised MP4 (moov atom at front)
        "-movflags", "+faststart",
        # Stop when the shorter stream ends (audio drives length)
        "-shortest",
        # Live progress to stdout; warnings/errors to stderr
        "-progress", "pipe:1",
        "-loglevel", "warning",
        str(out_path), "-y",
    ]

    proc = None
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,          # line-buffered
        )

        # ── Non-blocking readline() progress loop ──────────────────
        # FFmpeg writes "key=value\n" lines to stdout when -progress pipe:1
        # is used. readline() blocks only until a newline arrives — safe.
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break                               # process exited
            if not line:
                continue

            key, _, val = line.strip().partition("=")

            if key == "out_time_ms" and duration > 0:
                try:
                    secs  = int(val) / 1_000_000
                    pct   = min(99, int(secs / duration * 100))
                    job["progress"] = pct
                    elapsed = time.time() - job["start_time"]
                    if pct > 1:
                        job["eta"] = int((elapsed / pct) * (100 - pct))
                except (ValueError, ZeroDivisionError):
                    pass

            elif key == "progress" and val.strip() == "end":
                job["progress"] = 100

        proc.wait(timeout=7200)                     # 2-hour absolute cap

        if proc.returncode == 0 and out_path.exists():
            job["status"]      = "done"
            job["progress"]    = 100
            job["output_path"] = str(out_path)
            job["file_size"]   = round(out_path.stat().st_size / 1_048_576, 1)
            job["eta"]         = 0
        else:
            stderr_tail = proc.stderr.read(800) if proc.stderr else ""
            job["status"] = "error"
            job["error"]  = (
                f"FFmpeg exited with code {proc.returncode}. {stderr_tail.strip()}"
            )

    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        job["status"] = "error"
        job["error"]  = "Render timeout (> 2 hours). Please use a shorter audio file."

    except FileNotFoundError:
        job["status"] = "error"
        job["error"]  = "FFmpeg not found on server. Please contact support."

    except Exception as exc:
        job["status"] = "error"
        job["error"]  = str(exc)

    finally:
        # ── Immediately delete uploaded source files ────────────────
        # Done here regardless of success/failure to free disk space
        # on Render's ephemeral filesystem as soon as possible.
        for src in (img_path, aud_path):
            try:
                os.remove(src)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════
#  EMBEDDED FRONTEND  (glassmorphic, Emerald theme)
# ═══════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VideoForge — Cloud Render</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Sora:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
/* ── Reset ───────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:       #060b06;
  --surface:  #0c130c;
  --card:     #101a10;
  --glass:    rgba(16,26,16,.65);
  --border:   #1a2e1a;
  --border2:  #243424;
  --green:    #22c55e;
  --green2:   #4ade80;
  --green3:   #86efac;
  --dim:      #2d422d;
  --text:     #dff0df;
  --muted:    #617761;
  --danger:   #f87171;
  --r:        16px;
  --mono:     'Space Mono', monospace;
  --sans:     'Sora', sans-serif;
}

html, body { height: 100%; }
body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── Animated grid background ────────────────────────── */
body::before {
  content: '';
  position: fixed; inset: 0; z-index: 0;
  background-image:
    linear-gradient(rgba(34,197,94,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,197,94,.035) 1px, transparent 1px);
  background-size: 52px 52px;
  animation: gridDrift 24s linear infinite;
  pointer-events: none;
}
@keyframes gridDrift { to { background-position: 52px 52px; } }

/* Radial ambient glow */
body::after {
  content: '';
  position: fixed;
  top: -15%; left: 50%; transform: translateX(-50%);
  width: 900px; height: 560px;
  background: radial-gradient(ellipse, rgba(34,197,94,.10) 0%, transparent 68%);
  pointer-events: none; z-index: 0;
}

/* ── Layout ──────────────────────────────────────────── */
.wrapper {
  position: relative; z-index: 1;
  max-width: 660px;
  margin: 0 auto;
  padding: 44px 20px 64px;
}

/* ── Header ──────────────────────────────────────────── */
header { text-align: center; margin-bottom: 42px; }

.badge {
  display: inline-flex; align-items: center; gap: 8px;
  background: rgba(34,197,94,.08);
  border: 1px solid rgba(34,197,94,.22);
  border-radius: 100px;
  padding: 6px 18px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--green2);
  letter-spacing: 2.5px;
  text-transform: uppercase;
  margin-bottom: 20px;
}
.badge-dot {
  width: 7px; height: 7px;
  background: var(--green);
  border-radius: 50%;
  animation: blink 2.2s ease-in-out infinite;
}
@keyframes blink { 0%,100%{ opacity:1; } 50%{ opacity:.3; } }

h1 {
  font-size: clamp(26px, 5.5vw, 44px);
  font-weight: 700;
  letter-spacing: -1.5px;
  color: #fff;
  line-height: 1.08;
}
h1 em {
  font-style: normal;
  background: linear-gradient(130deg, var(--green) 0%, var(--green2) 55%, var(--green3) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.subtitle {
  margin-top: 12px;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.6;
}

/* ── Glassmorphic card ───────────────────────────────── */
.card {
  background: var(--glass);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 24px;
  margin-bottom: 14px;
  transition: border-color .2s, box-shadow .2s;
}
.card:hover {
  border-color: var(--border2);
  box-shadow: 0 0 0 1px rgba(34,197,94,.06) inset, 0 8px 32px rgba(0,0,0,.35);
}

.card-label {
  font-size: 10px;
  font-family: var(--mono);
  color: var(--green);
  letter-spacing: 2.5px;
  text-transform: uppercase;
  margin-bottom: 16px;
  display: flex; align-items: center; gap: 10px;
}
.card-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }

/* ── Drop zone ───────────────────────────────────────── */
.drop-zone {
  border: 1.5px dashed var(--dim);
  border-radius: 10px;
  padding: 30px 20px;
  text-align: center;
  cursor: pointer;
  transition: all .2s;
  position: relative;
  overflow: hidden;
  user-select: none;
}
.drop-zone:hover, .drop-zone.drag-over {
  border-color: var(--green);
  background: rgba(34,197,94,.06);
  box-shadow: 0 0 24px rgba(34,197,94,.08) inset;
}
.drop-zone input[type="file"] {
  position: absolute; inset: 0;
  opacity: 0; cursor: pointer;
  width: 100%; height: 100%;
}
.drop-icon { font-size: 30px; margin-bottom: 8px; }
.drop-text { color: var(--muted); font-size: 13.5px; }
.drop-text strong { color: var(--green2); font-weight: 600; }
.drop-hint { font-size: 11px; color: var(--dim); margin-top: 5px; font-family: var(--mono); letter-spacing: .5px; }

/* ── Image preview ───────────────────────────────────── */
#imgPreview {
  display: none;
  margin-top: 14px;
  border-radius: 10px;
  overflow: hidden;
  position: relative;
  border: 1px solid var(--border2);
}
#imgPreview img {
  width: 100%; max-height: 190px; object-fit: cover; display: block;
}
#imgPreview .overlay {
  position: absolute; bottom: 0; left: 0; right: 0;
  background: linear-gradient(transparent, rgba(0,0,0,.72));
  padding: 12px 14px 10px;
  font-size: 11.5px; color: #aaa; font-family: var(--mono);
}

/* ── Audio pill ──────────────────────────────────────── */
#audioInfo {
  display: none;
  margin-top: 14px;
  background: rgba(6,11,6,.7);
  border: 1px solid var(--border2);
  border-radius: 10px;
  padding: 14px 16px;
}
.aud-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
}
.aud-cell { display: flex; flex-direction: column; gap: 3px; }
.aud-cell .lbl { color: var(--muted); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; }
.aud-cell .val { color: var(--green2); font-weight: 700; word-break: break-all; }
.aud-wave {
  margin-top: 12px; height: 3px;
  background: var(--border);
  border-radius: 99px; overflow: hidden;
}
.aud-wave-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--green), var(--green2));
  border-radius: 99px;
  animation: waveFlow 2.5s ease-in-out infinite;
  width: 55%;
}
@keyframes waveFlow { 0%,100%{ opacity:.5; transform:scaleX(1); } 50%{ opacity:1; transform:scaleX(1.04); } }

/* ── Quality selector ────────────────────────────────── */
.q-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
}
.q-btn {
  background: rgba(6,11,6,.8);
  border: 1.5px solid var(--border);
  border-radius: 10px;
  padding: 14px 8px;
  cursor: pointer;
  text-align: center;
  transition: all .18s;
  font-family: var(--sans);
}
.q-btn:hover { border-color: var(--dim); background: rgba(34,197,94,.04); }
.q-btn.active {
  border-color: var(--green);
  background: rgba(34,197,94,.1);
  box-shadow: 0 0 18px rgba(34,197,94,.12) inset;
}
.q-icon { font-size: 22px; margin-bottom: 5px; }
.q-name { font-size: 13px; font-weight: 600; color: var(--text); }
.q-desc { font-size: 11px; color: var(--muted); margin-top: 2px; font-family: var(--mono); }
input[name="quality"] { display: none; }

/* ── Render button ───────────────────────────────────── */
#renderBtn {
  width: 100%; padding: 17px;
  background: linear-gradient(135deg, #15803d 0%, #22c55e 100%);
  border: none; border-radius: var(--r);
  color: #fff;
  font-family: var(--sans);
  font-size: 16px; font-weight: 700;
  cursor: pointer; letter-spacing: .4px;
  transition: all .22s;
  position: relative; overflow: hidden;
  margin-top: 10px;
  box-shadow: 0 4px 20px rgba(34,197,94,.2);
}
#renderBtn::before {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(135deg, transparent 25%, rgba(255,255,255,.14) 50%, transparent 75%);
  transform: translateX(-120%);
  transition: transform .5s;
}
#renderBtn:hover::before { transform: translateX(120%); }
#renderBtn:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(34,197,94,.38); }
#renderBtn:active { transform: translateY(0); box-shadow: 0 4px 16px rgba(34,197,94,.2); }
#renderBtn:disabled {
  background: var(--dim); cursor: not-allowed;
  transform: none; box-shadow: none;
}

/* ── Progress card ───────────────────────────────────── */
#progressSection {
  display: none;
  background: var(--glass);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 26px;
  margin-top: 16px;
}
.prog-row {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 18px;
}
.prog-title { font-weight: 600; font-size: 15px; }
.prog-pct {
  font-family: var(--mono);
  font-size: 26px; font-weight: 700;
  color: var(--green2);
  letter-spacing: -1px;
}
.prog-track {
  height: 8px;
  background: var(--surface);
  border-radius: 99px; overflow: hidden;
  border: 1px solid var(--border);
}
.prog-fill {
  height: 100%; border-radius: 99px;
  background: linear-gradient(90deg, #15803d, var(--green2));
  transition: width .6s cubic-bezier(.4,0,.2,1);
  width: 0%;
  position: relative;
}
.prog-fill::after {
  content: '';
  position: absolute; top: 0; right: 0; bottom: 0; width: 60px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.3));
  animation: sheen 1.4s ease-in-out infinite;
}
@keyframes sheen { 0%,100%{ opacity:0; } 50%{ opacity:1; } }
.prog-meta {
  display: flex; justify-content: space-between;
  margin-top: 12px;
  font-family: var(--mono); font-size: 11.5px; color: var(--muted);
}
.prog-status {
  margin-top: 12px;
  padding: 10px 14px;
  background: rgba(6,11,6,.8);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12.5px; color: var(--muted);
  font-family: var(--mono);
  min-height: 38px;
}

/* ── Download card ───────────────────────────────────── */
#downloadSection {
  display: none;
  background: rgba(34,197,94,.07);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  border: 1px solid rgba(34,197,94,.28);
  border-radius: var(--r);
  padding: 30px 24px;
  text-align: center;
  margin-top: 16px;
  animation: fadeUp .42s cubic-bezier(.4,0,.2,1);
}
@keyframes fadeUp { from{ opacity:0; transform:translateY(10px); } to{ opacity:1; transform:translateY(0); } }
.done-icon { font-size: 44px; margin-bottom: 12px; }
.done-title { font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 4px; }
.done-meta { font-size: 12.5px; color: var(--muted); font-family: var(--mono); margin-bottom: 22px; }

#downloadBtn {
  display: inline-flex; align-items: center; gap: 10px;
  background: var(--green);
  color: #000;
  font-weight: 700; font-size: 15px;
  padding: 14px 30px;
  border-radius: 100px;
  text-decoration: none;
  transition: all .2s;
  box-shadow: 0 4px 18px rgba(34,197,94,.35);
}
#downloadBtn:hover {
  background: var(--green2);
  transform: translateY(-2px);
  box-shadow: 0 8px 28px rgba(34,197,94,.45);
}

/* ── Error card ──────────────────────────────────────── */
#errorSection {
  display: none;
  background: rgba(248,113,113,.07);
  border: 1px solid rgba(248,113,113,.28);
  border-radius: var(--r);
  padding: 22px;
  margin-top: 16px;
  animation: fadeUp .4s ease;
}
.err-title { font-weight: 600; color: var(--danger); margin-bottom: 8px; font-size: 15px; }
.err-msg { font-family: var(--mono); font-size: 12px; color: #fca5a5; word-break: break-all; line-height: 1.7; }

/* ── Shared reset link ───────────────────────────────── */
.reset-link {
  display: block; margin-top: 16px;
  background: none; border: none;
  color: var(--muted); font-size: 13px; cursor: pointer;
  font-family: var(--sans);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.reset-link:hover { color: var(--text); }

/* ── Toast ───────────────────────────────────────────── */
.toast {
  position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
  background: var(--card); border: 1px solid var(--border2);
  padding: 11px 22px; border-radius: 100px;
  font-size: 13px; color: var(--text);
  pointer-events: none; opacity: 0; transition: opacity .28s;
  z-index: 999;
}
.toast.show { opacity: 1; }

/* ── Footer ──────────────────────────────────────────── */
footer {
  text-align: center;
  margin-top: 48px;
  font-size: 11.5px;
  color: var(--dim);
  font-family: var(--mono);
  letter-spacing: .5px;
}
</style>
</head>
<body>
<div class="wrapper">

  <!-- ════════════ HEADER ════════════ -->
  <header>
    <div class="badge"><span class="badge-dot"></span>VideoForge Cloud</div>
    <h1>Image + Audio<br>= <em>Video, Instant.</em></h1>
    <p class="subtitle">Drop your files below. FFmpeg renders it on the server — no re-encoding, no waiting.</p>
  </header>

  <!-- ════════════ FORM ════════════ -->
  <div id="formSection">

    <!-- Cover Image -->
    <div class="card">
      <div class="card-label">01 — Cover Image</div>
      <div class="drop-zone" id="imgDrop">
        <input type="file" id="imgInput" accept=".jpg,.jpeg,.png,.webp,.bmp">
        <div class="drop-icon">🖼️</div>
        <div class="drop-text"><strong>Click or drag</strong> an image here</div>
        <div class="drop-hint">JPG · PNG · WEBP · BMP</div>
      </div>
      <div id="imgPreview">
        <img id="imgThumb" src="" alt="">
        <div class="overlay" id="imgMeta">—</div>
      </div>
    </div>

    <!-- Audio -->
    <div class="card">
      <div class="card-label">02 — Audio Track</div>
      <div class="drop-zone" id="audDrop">
        <input type="file" id="audInput" accept=".mp3,.wav,.m4a,.aac,.ogg,.flac">
        <div class="drop-icon">🎵</div>
        <div class="drop-text"><strong>Click or drag</strong> audio here</div>
        <div class="drop-hint">MP3 · WAV · M4A · AAC · OGG · FLAC &nbsp;|&nbsp; Max 150 MB</div>
      </div>
      <div id="audioInfo">
        <div class="aud-grid">
          <div class="aud-cell"><span class="lbl">File</span><span class="val" id="audName">—</span></div>
          <div class="aud-cell"><span class="lbl">Size</span><span class="val" id="audSize">—</span></div>
          <div class="aud-cell"><span class="lbl">Format</span><span class="val" id="audFmt">—</span></div>
        </div>
        <div class="aud-wave"><div class="aud-wave-fill"></div></div>
      </div>
    </div>

    <!-- Quality -->
    <div class="card">
      <div class="card-label">03 — Output Quality</div>
      <div class="q-grid">
        <label class="q-btn" id="qFast" onclick="setQuality('fast',this)">
          <input type="radio" name="quality" value="fast">
          <div class="q-icon">⚡</div>
          <div class="q-name">Fast</div>
          <div class="q-desc">ultrafast</div>
        </label>
        <label class="q-btn active" id="qBalanced" onclick="setQuality('balanced',this)">
          <input type="radio" name="quality" value="balanced" checked>
          <div class="q-icon">⚖️</div>
          <div class="q-name">Balanced</div>
          <div class="q-desc">recommended</div>
        </label>
        <label class="q-btn" id="qHq" onclick="setQuality('hq',this)">
          <input type="radio" name="quality" value="hq">
          <div class="q-icon">💎</div>
          <div class="q-name">High Quality</div>
          <div class="q-desc">best output</div>
        </label>
      </div>
    </div>

    <button id="renderBtn" onclick="startRender()">⚡ Render Video</button>
  </div><!-- /formSection -->

  <!-- ════════════ PROGRESS ════════════ -->
  <div id="progressSection">
    <div class="prog-row">
      <span class="prog-title">🎬 Rendering…</span>
      <span class="prog-pct" id="progPct">0%</span>
    </div>
    <div class="prog-track">
      <div class="prog-fill" id="progBar"></div>
    </div>
    <div class="prog-meta">
      <span id="progElapsed">0s elapsed</span>
      <span id="progEta">ETA: —</span>
    </div>
    <div class="prog-status" id="progStatus">Queued…</div>
  </div>

  <!-- ════════════ DOWNLOAD ════════════ -->
  <div id="downloadSection">
    <div class="done-icon">✅</div>
    <div class="done-title">Video Ready!</div>
    <div class="done-meta" id="doneMeta">—</div>
    <a id="downloadBtn" href="#" download>⬇️ Download MP4</a>
    <button class="reset-link" onclick="resetUI()">Make another video</button>
  </div>

  <!-- ════════════ ERROR ════════════ -->
  <div id="errorSection">
    <div class="err-title">❌ Render Failed</div>
    <div class="err-msg" id="errMsg">Unknown error</div>
    <button class="reset-link" onclick="resetUI()">Try again</button>
  </div>

</div><!-- /wrapper -->

<div class="toast" id="toast"></div>
<footer>VideoForge &nbsp;·&nbsp; FFmpeg-powered &nbsp;·&nbsp; -c:a copy &nbsp;·&nbsp; Render.com Free Tier</footer>

<script>
// ── State ──────────────────────────────────────────────────────────
let activeJobId    = null;
let pollTimer      = null;
let renderStart    = null;
let selectedQuality = 'balanced';

// ── Quality selector ───────────────────────────────────────────────
function setQuality(q, el) {
  selectedQuality = q;
  document.querySelectorAll('.q-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}

// ── Generic drag-and-drop setup ────────────────────────────────────
function setupDrop(zoneId, inputId, onFile) {
  const zone  = document.getElementById(zoneId);
  const input = document.getElementById(inputId);

  zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', ()=> zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) { onFile(file); input.files = e.dataTransfer.files; }
  });
  input.addEventListener('change', () => { if (input.files[0]) onFile(input.files[0]); });
}

// ── Image: live thumbnail ──────────────────────────────────────────
setupDrop('imgDrop', 'imgInput', file => {
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('imgThumb').src = e.target.result;
    document.getElementById('imgMeta').textContent =
      file.name + '  ·  ' + (file.size / 1048576).toFixed(1) + ' MB';
    document.getElementById('imgPreview').style.display = 'block';
  };
  reader.readAsDataURL(file);
});

// ── Audio: metadata pill ───────────────────────────────────────────
setupDrop('audDrop', 'audInput', file => {
  const nameEl = document.getElementById('audName');
  nameEl.textContent = file.name.length > 20
    ? file.name.slice(0, 18) + '…'
    : file.name;
  document.getElementById('audSize').textContent = (file.size / 1048576).toFixed(1) + ' MB';
  document.getElementById('audFmt').textContent  = file.name.split('.').pop().toUpperCase();
  document.getElementById('audioInfo').style.display = 'block';
});

// ── Start render ───────────────────────────────────────────────────
async function startRender() {
  const imgFile = document.getElementById('imgInput').files[0];
  const audFile = document.getElementById('audInput').files[0];

  if (!imgFile) { showToast('⚠️ Please select a cover image'); return; }
  if (!audFile) { showToast('⚠️ Please select an audio file'); return; }

  const form = new FormData();
  form.append('image',   imgFile);
  form.append('audio',   audFile);
  form.append('quality', selectedQuality);

  setBtn(false, '⏳ Uploading…');

  try {
    const res  = await fetch('/render', { method: 'POST', body: form });
    const data = await res.json();

    if (!res.ok || data.error) {
      showError(data.error || 'Upload failed — try again.');
      resetBtn(); return;
    }

    activeJobId = data.job_id;
    renderStart = Date.now();
    showSection('progressSection');
    pollStatus();

  } catch (e) {
    showError('Network error: ' + e.message);
    resetBtn();
  }
}

// ── Poll /status/<id> every 1.4 s ─────────────────────────────────
async function pollStatus() {
  if (!activeJobId) return;
  try {
    const res  = await fetch('/status/' + activeJobId);
    const data = await res.json();
    updateProgress(data);

    if (data.status === 'done')  { onDone(data);        return; }
    if (data.status === 'error') { showError(data.error); return; }

    pollTimer = setTimeout(pollStatus, 1400);
  } catch (_) {
    pollTimer = setTimeout(pollStatus, 3200);   // retry on fluke
  }
}

// ── Progress display ───────────────────────────────────────────────
function updateProgress(data) {
  const pct = data.progress || 0;
  document.getElementById('progBar').style.width = pct + '%';
  document.getElementById('progPct').textContent = pct + '%';

  const el = Math.round((Date.now() - renderStart) / 1000);
  document.getElementById('progElapsed').textContent = fmt(el) + ' elapsed';
  if (data.eta > 0)
    document.getElementById('progEta').textContent = 'ETA: ' + fmt(data.eta);

  const map = {
    queued:    '⏳ Queued — waiting for render slot…',
    rendering: `⚙️  Encoding video… ${pct}% complete`,
    done:      '✅ Render complete!',
    error:     '❌ Render failed.',
  };
  document.getElementById('progStatus').textContent = map[data.status] || data.status;
}

// ── Done → show download ───────────────────────────────────────────
function onDone(data) {
  clearTimeout(pollTimer);
  showSection('downloadSection');

  const el = Math.round((Date.now() - renderStart) / 1000);
  document.getElementById('doneMeta').textContent =
    (data.file_size || '?') + ' MB  ·  Rendered in ' + fmt(el);

  const btn = document.getElementById('downloadBtn');
  btn.href     = '/download/' + activeJobId;
  btn.download = 'VideoForge_Output.mp4';

  // Auto-trigger browser download after short delay
  setTimeout(() => btn.click(), 700);
}

// ── Error display ──────────────────────────────────────────────────
function showError(msg) {
  clearTimeout(pollTimer);
  showSection('errorSection');
  document.getElementById('errMsg').textContent = msg || 'Unknown error.';
  resetBtn();
}

// ── UI helpers ─────────────────────────────────────────────────────
function showSection(id) {
  ['progressSection','downloadSection','errorSection'].forEach(s => {
    document.getElementById(s).style.display = s === id ? 'block' : 'none';
  });
}
function setBtn(enabled, label) {
  const b = document.getElementById('renderBtn');
  b.disabled = !enabled;
  b.textContent = label;
}
function resetBtn() { setBtn(true, '⚡ Render Video'); }
function resetUI() {
  clearTimeout(pollTimer);
  activeJobId = null;
  document.getElementById('progressSection').style.display = 'none';
  document.getElementById('downloadSection').style.display = 'none';
  document.getElementById('errorSection').style.display    = 'none';
  document.getElementById('progBar').style.width = '0%';
  document.getElementById('progPct').textContent = '0%';
  resetBtn();
}
function fmt(s) {
  return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/render", methods=["POST"])
def render():
    """
    Validates and saves uploaded files, creates a job record,
    fires a background render thread, returns { job_id }.
    """
    if "image" not in request.files or "audio" not in request.files:
        return jsonify({"error": "Both image and audio files are required."}), 400

    img_file = request.files["image"]
    aud_file = request.files["audio"]
    quality  = request.form.get("quality", "balanced")

    img_ext = Path(img_file.filename or "").suffix.lower()
    aud_ext = Path(aud_file.filename or "").suffix.lower()

    if img_ext not in ALLOWED_IMAGE_EXT:
        return jsonify({
            "error": f"Unsupported image format '{img_ext}'. Allowed: JPG, PNG, WEBP, BMP"
        }), 400

    if aud_ext not in ALLOWED_AUDIO_EXT:
        return jsonify({
            "error": f"Unsupported audio format '{aud_ext}'. Allowed: MP3, WAV, M4A, AAC, OGG, FLAC"
        }), 400

    if quality not in QUALITY_PRESETS:
        quality = "balanced"

    # ── Persist uploads to a per-job temp directory ──────────────
    job_id  = str(uuid.uuid4())[:10]
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    img_path = job_dir / f"src{img_ext}"
    aud_path = job_dir / f"src{aud_ext}"
    out_path = job_dir / "output.mp4"

    img_file.save(img_path)
    aud_file.save(aud_path)

    # ── Register job ─────────────────────────────────────────────
    jobs[job_id] = {
        "status":      "queued",
        "progress":    0,
        "eta":         None,
        "error":       None,
        "filename":    Path(aud_file.filename or "video").stem,
        "output_path": None,
        "file_size":   None,
        "start_time":  None,
        "created":     time.time(),
    }

    # ── Launch worker ────────────────────────────────────────────
    threading.Thread(
        target=render_worker,
        args=(job_id, img_path, aud_path, out_path, quality),
        daemon=True,
        name=f"render-{job_id}",
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str):
    """Polled by the frontend every ~1.4 s; returns current job state."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    elapsed = 0
    if job.get("start_time"):
        elapsed = int(time.time() - job["start_time"])

    return jsonify({
        "status":    job["status"],
        "progress":  job["progress"],
        "eta":       job.get("eta"),
        "error":     job.get("error"),
        "elapsed":   elapsed,
        "file_size": job.get("file_size"),
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    """
    Streams the rendered MP4 to the browser, then schedules
    deletion of the output file 60 s later to reclaim disk space.
    """
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return "File not ready yet.", 404

    out_path = Path(job.get("output_path", ""))
    if not out_path.exists():
        return "Output file not found on server.", 404

    safe_name = f"{job.get('filename', 'video')[:40]}_rendered.mp4"

    # ── Schedule post-download cleanup (60 s grace period) ───────
    def _delete_after(path: Path, delay: int = 60) -> None:
        time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()         # remove job dir if empty
        except OSError:
            pass

    threading.Thread(
        target=_delete_after, args=(out_path,), daemon=True, name=f"gc-{job_id}"
    ).start()

    return send_file(str(out_path), as_attachment=True, download_name=safe_name)


# ═══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[VideoForge] Starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
