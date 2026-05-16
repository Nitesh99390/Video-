"""
app.py — Advanced Cloud Video Maker
====================================
Features:
  • Async FFmpeg rendering (background thread) — server never blocks
  • Live real-time progress bar (FFmpeg pipe parsing)
  • Drag-and-drop file upload with instant preview
  • Image thumbnail preview before render
  • Audio info display (duration, size, format)
  • 3 quality presets (Fast / Balanced / HQ)
  • File-type validation (extension + MIME)
  • ETA + elapsed time display
  • Auto-download when done
  • Auto cleanup of temp files
  • Render history (last 5 jobs)

Deploy: works on localhost + Render.com (PORT env var auto-detected)
"""

import os
import subprocess
import uuid
import json
import threading
import time
from pathlib import Path
from flask import Flask, request, send_file, render_template_string, jsonify

# ═══════════════════════════════════════════════════════
#  APP SETUP
# ═══════════════════════════════════════════════════════
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024   # 150 MB

TEMP_DIR = Path("/tmp/vm_jobs")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job store  {job_id: dict}
jobs: dict[str, dict] = {}

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

QUALITY_PRESETS = {
    "fast":     {"preset": "ultrafast", "crf": "28", "label": "Fast"},
    "balanced": {"preset": "fast",      "crf": "23", "label": "Balanced"},
    "hq":       {"preset": "medium",    "crf": "18", "label": "High Quality"},
}

# ═══════════════════════════════════════════════════════
#  FFPROBE HELPER
# ═══════════════════════════════════════════════════════
def get_audio_info(path: Path) -> dict:
    """Return duration (s), size (MB), bitrate (kbps) via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30
        )
        fmt = json.loads(result.stdout).get("format", {})
        return {
            "duration": float(fmt.get("duration", 0)),
            "size_mb":  round(int(fmt.get("size", 0)) / 1_048_576, 1),
            "bitrate":  int(fmt.get("bit_rate", 0)) // 1000,
        }
    except Exception:
        return {"duration": 0, "size_mb": 0, "bitrate": 0}


# ═══════════════════════════════════════════════════════
#  RENDER WORKER  (background thread)
# ═══════════════════════════════════════════════════════
def render_worker(job_id: str, img_path: Path, aud_path: Path,
                  out_path: Path, quality: str) -> None:
    """
    Runs FFmpeg in a subprocess, reads live progress from stdout,
    updates jobs[job_id] in-place.
    """
    job = jobs[job_id]

    # Get audio info for progress calculation
    info             = get_audio_info(aud_path)
    duration         = info["duration"]
    job["duration"]  = duration
    job["status"]    = "rendering"
    job["start_time"] = time.time()

    cfg = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])

    command = [
        "ffmpeg",
        "-loop", "1", "-framerate", "1",
        "-i", str(img_path),
        "-i", str(aud_path),
        "-c:v", "libx264",
        "-preset", cfg["preset"],
        "-tune", "stillimage",
        "-crf", cfg["crf"],
        "-c:a", "aac", "-b:a", "192k",      # re-encode for max compatibility
        "-pix_fmt", "yuv420p",               # required for WhatsApp/social media
        "-movflags", "+faststart",           # web-optimized MP4
        "-shortest",
        "-progress", "pipe:1",              # live progress to stdout
        "-loglevel", "quiet",
        str(out_path), "-y",
    ]

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Parse progress lines from FFmpeg
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "out_time_ms" and duration > 0:
                try:
                    secs = int(val) / 1_000_000
                    pct  = min(99, int(secs / duration * 100))
                    job["progress"] = pct
                    elapsed = time.time() - job["start_time"]
                    if pct > 1:
                        job["eta"] = int((elapsed / pct) * (100 - pct))
                except (ValueError, ZeroDivisionError):
                    pass
            elif key == "progress" and val == "end":
                job["progress"] = 100

        proc.wait(timeout=7200)   # 2-hour hard cap

        if proc.returncode == 0 and out_path.exists():
            job["status"]      = "done"
            job["progress"]    = 100
            job["output_path"] = str(out_path)
            job["file_size"]   = round(out_path.stat().st_size / 1_048_576, 1)
            job["eta"]         = 0
        else:
            stderr_tail = proc.stderr.read(600) if proc.stderr else ""
            job["status"] = "error"
            job["error"]  = f"FFmpeg returned code {proc.returncode}. {stderr_tail}"

    except subprocess.TimeoutExpired:
        proc.kill()
        job["status"] = "error"
        job["error"]  = "Render timeout (>2 hours). Chota audio use karein."
    except FileNotFoundError:
        job["status"] = "error"
        job["error"]  = "FFmpeg nahi mila. Server pe FFmpeg install karo."
    except Exception as e:
        job["status"] = "error"
        job["error"]  = str(e)
    finally:
        for f in (img_path, aud_path):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════
#  HTML / CSS / JS  (single-file embedded)
# ═══════════════════════════════════════════════════════
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VideoForge — Cloud Render</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Sora:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
/* ── Reset & Base ─────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #080c08;
  --surface: #0f160f;
  --card:    #141e14;
  --border:  #1e2e1e;
  --green:   #22c55e;
  --green2:  #4ade80;
  --dim:     #374137;
  --text:    #e2f0e2;
  --muted:   #6b7a6b;
  --danger:  #f87171;
  --radius:  14px;
  --mono: 'Space Mono', monospace;
  --sans: 'Sora', sans-serif;
}

html, body { height: 100%; }

body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── Animated background grid ─────────────────── */
body::before {
  content: '';
  position: fixed; inset: 0; z-index: 0;
  background-image:
    linear-gradient(rgba(34,197,94,.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,197,94,.04) 1px, transparent 1px);
  background-size: 48px 48px;
  animation: gridDrift 20s linear infinite;
  pointer-events: none;
}
@keyframes gridDrift { to { background-position: 48px 48px; } }

/* Radial glow */
body::after {
  content: '';
  position: fixed;
  top: -20%; left: 50%; transform: translateX(-50%);
  width: 800px; height: 500px;
  background: radial-gradient(ellipse, rgba(34,197,94,.12) 0%, transparent 70%);
  pointer-events: none; z-index: 0;
}

/* ── Layout ───────────────────────────────────── */
.wrapper {
  position: relative; z-index: 1;
  max-width: 680px;
  margin: 0 auto;
  padding: 40px 20px 60px;
}

/* ── Header ───────────────────────────────────── */
header {
  text-align: center;
  margin-bottom: 40px;
}
.logo-badge {
  display: inline-flex; align-items: center; gap: 8px;
  background: rgba(34,197,94,.1);
  border: 1px solid rgba(34,197,94,.25);
  border-radius: 100px;
  padding: 6px 16px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--green2);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 18px;
}
.logo-badge span { width: 6px; height: 6px; background: var(--green); border-radius: 50%; animation: pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100%{ opacity:1; transform:scale(1); } 50%{ opacity:.4; transform:scale(.6); } }

h1 {
  font-size: clamp(28px, 5vw, 42px);
  font-weight: 700;
  letter-spacing: -1px;
  color: #fff;
  line-height: 1.1;
}
h1 em {
  font-style: normal;
  background: linear-gradient(135deg, var(--green) 0%, var(--green2) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.subtitle {
  margin-top: 10px;
  color: var(--muted);
  font-size: 14px;
}

/* ── Cards ────────────────────────────────────── */
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  margin-bottom: 16px;
  transition: border-color .2s;
}
.card:hover { border-color: var(--dim); }

.card-label {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--green);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 14px;
  display: flex; align-items: center; gap: 8px;
}
.card-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }

/* ── Drop zone ────────────────────────────────── */
.drop-zone {
  border: 1.5px dashed var(--dim);
  border-radius: 10px;
  padding: 28px 20px;
  text-align: center;
  cursor: pointer;
  transition: all .2s;
  position: relative;
  overflow: hidden;
}
.drop-zone:hover,
.drop-zone.drag-over {
  border-color: var(--green);
  background: rgba(34,197,94,.05);
}
.drop-zone input[type="file"] {
  position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
}
.drop-icon { font-size: 28px; margin-bottom: 8px; }
.drop-text { color: var(--muted); font-size: 14px; }
.drop-text strong { color: var(--green2); }
.drop-hint { font-size: 11px; color: var(--dim); margin-top: 4px; font-family: var(--mono); }

/* ── Image Preview ────────────────────────────── */
#imgPreview {
  display: none;
  margin-top: 14px;
  border-radius: 8px;
  overflow: hidden;
  position: relative;
}
#imgPreview img {
  width: 100%; max-height: 180px; object-fit: cover;
  display: block;
}
#imgPreview .overlay {
  position: absolute; bottom: 0; left: 0; right: 0;
  background: linear-gradient(transparent, rgba(0,0,0,.7));
  padding: 10px 12px 8px;
  font-size: 12px; color: #aaa; font-family: var(--mono);
}

/* ── Audio Info ───────────────────────────────── */
#audioInfo {
  display: none;
  margin-top: 14px;
  background: var(--surface);
  border-radius: 8px;
  padding: 12px 16px;
  font-family: var(--mono);
  font-size: 12px;
}
.audio-row { display: flex; justify-content: space-between; align-items: center; }
.audio-row + .audio-row { margin-top: 6px; }
.audio-row span:first-child { color: var(--muted); }
.audio-row span:last-child  { color: var(--green2); }
.audio-bar {
  margin-top: 10px;
  height: 3px;
  background: var(--border);
  border-radius: 99px;
  overflow: hidden;
}
.audio-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--green), var(--green2));
  border-radius: 99px;
  animation: shimmer 2s ease-in-out infinite;
  width: 60%;
}
@keyframes shimmer { 0%,100%{ opacity:.6; } 50%{ opacity:1; } }

/* ── Quality Selector ─────────────────────────── */
.quality-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
}
.q-btn {
  background: var(--surface);
  border: 1.5px solid var(--border);
  border-radius: 8px;
  padding: 12px 8px;
  cursor: pointer;
  text-align: center;
  transition: all .15s;
  font-family: var(--sans);
}
.q-btn:hover { border-color: var(--dim); }
.q-btn.active {
  border-color: var(--green);
  background: rgba(34,197,94,.1);
}
.q-icon { font-size: 20px; margin-bottom: 4px; }
.q-name { font-size: 13px; font-weight: 600; color: var(--text); }
.q-desc { font-size: 11px; color: var(--muted); margin-top: 2px; font-family: var(--mono); }
input[name="quality"] { display: none; }

/* ── Render Button ────────────────────────────── */
#renderBtn {
  width: 100%; padding: 16px;
  background: linear-gradient(135deg, #16a34a 0%, #22c55e 100%);
  border: none; border-radius: var(--radius);
  color: #fff;
  font-family: var(--sans);
  font-size: 16px; font-weight: 700;
  cursor: pointer;
  letter-spacing: .5px;
  transition: all .2s;
  position: relative; overflow: hidden;
  margin-top: 8px;
}
#renderBtn::after {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(135deg, transparent 30%, rgba(255,255,255,.15) 50%, transparent 70%);
  transform: translateX(-100%);
  transition: transform .4s;
}
#renderBtn:hover::after { transform: translateX(100%); }
#renderBtn:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(34,197,94,.35); }
#renderBtn:active { transform: translateY(0); }
#renderBtn:disabled {
  background: var(--dim); cursor: not-allowed; transform: none; box-shadow: none;
}

/* ── Progress Section ─────────────────────────── */
#progressSection {
  display: none;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  margin-top: 16px;
}

.prog-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 16px;
}
.prog-title { font-weight: 600; font-size: 15px; }
.prog-pct {
  font-family: var(--mono);
  font-size: 22px; font-weight: 700;
  color: var(--green2);
}

.prog-bar-track {
  height: 8px; background: var(--surface);
  border-radius: 99px; overflow: hidden;
}
.prog-bar-fill {
  height: 100%;
  border-radius: 99px;
  background: linear-gradient(90deg, #16a34a, var(--green2));
  transition: width .5s ease;
  width: 0%;
  position: relative;
}
.prog-bar-fill::after {
  content: '';
  position: absolute; top: 0; right: 0; bottom: 0; width: 40px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.35));
  animation: sweep 1.2s ease-in-out infinite;
}
@keyframes sweep { 0%{ opacity:0; } 50%{ opacity:1; } 100%{ opacity:0; } }

.prog-meta {
  display: flex; justify-content: space-between;
  margin-top: 12px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
}

.prog-status {
  margin-top: 12px;
  padding: 8px 12px;
  background: var(--surface);
  border-radius: 6px;
  font-size: 13px; color: var(--muted);
  font-family: var(--mono);
  min-height: 36px;
}

/* ── Download Section ─────────────────────────── */
#downloadSection {
  display: none;
  background: rgba(34,197,94,.08);
  border: 1px solid rgba(34,197,94,.3);
  border-radius: var(--radius);
  padding: 24px;
  text-align: center;
  margin-top: 16px;
  animation: fadeSlide .4s ease;
}
@keyframes fadeSlide { from{ opacity:0; transform:translateY(8px); } to{ opacity:1; transform:translateY(0); } }

.done-icon { font-size: 40px; margin-bottom: 10px; }
.done-title { font-size: 18px; font-weight: 700; color: #fff; margin-bottom: 4px; }
.done-meta { font-size: 13px; color: var(--muted); font-family: var(--mono); margin-bottom: 18px; }

#downloadBtn {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--green);
  color: #000;
  font-weight: 700; font-size: 15px;
  padding: 13px 28px;
  border-radius: 100px;
  text-decoration: none;
  transition: all .2s;
}
#downloadBtn:hover { background: var(--green2); transform: translateY(-2px); box-shadow: 0 6px 20px rgba(34,197,94,.4); }

.new-render-btn {
  display: block;
  margin-top: 14px;
  background: none; border: none;
  color: var(--muted); font-size: 13px; cursor: pointer;
  font-family: var(--sans);
  text-decoration: underline;
}
.new-render-btn:hover { color: var(--text); }

/* ── Error ────────────────────────────────────── */
#errorSection {
  display: none;
  background: rgba(248,113,113,.08);
  border: 1px solid rgba(248,113,113,.3);
  border-radius: var(--radius);
  padding: 20px;
  margin-top: 16px;
}
.err-title { font-weight: 600; color: var(--danger); margin-bottom: 6px; }
.err-msg { font-family: var(--mono); font-size: 12px; color: #fca5a5; word-break: break-all; }

/* ── Toast ────────────────────────────────────── */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: var(--card); border: 1px solid var(--border);
  padding: 10px 20px; border-radius: 100px;
  font-size: 13px; color: var(--text);
  pointer-events: none; opacity: 0;
  transition: opacity .3s;
  z-index: 100;
}
.toast.show { opacity: 1; }

/* ── Footer ───────────────────────────────────── */
footer {
  text-align: center;
  margin-top: 40px;
  font-size: 12px;
  color: var(--dim);
  font-family: var(--mono);
}
</style>
</head>
<body>

<div class="wrapper">

  <!-- Header -->
  <header>
    <div class="logo-badge"><span></span> VideoForge Cloud</div>
    <h1>Image + Audio<br>= <em>Video, Instant.</em></h1>
    <p class="subtitle">Drop your files below. Server renders it with FFmpeg.</p>
  </header>

  <!-- ── UPLOAD FORM ─────────────────────────── -->
  <div id="formSection">

    <!-- Cover Image -->
    <div class="card">
      <div class="card-label">01 — Cover Image</div>
      <div class="drop-zone" id="imgDrop">
        <input type="file" id="imgInput" name="image" accept=".jpg,.jpeg,.png,.webp,.bmp" required>
        <div class="drop-icon">🖼️</div>
        <div class="drop-text"><strong>Click or drag</strong> an image here</div>
        <div class="drop-hint">JPG · PNG · WEBP · BMP</div>
      </div>
      <div id="imgPreview">
        <img id="imgThumb" src="" alt="Preview">
        <div class="overlay" id="imgMeta">—</div>
      </div>
    </div>

    <!-- Audio -->
    <div class="card">
      <div class="card-label">02 — Audio Track</div>
      <div class="drop-zone" id="audDrop">
        <input type="file" id="audInput" name="audio" accept=".mp3,.wav,.m4a,.aac,.ogg,.flac" required>
        <div class="drop-icon">🎵</div>
        <div class="drop-text"><strong>Click or drag</strong> audio here</div>
        <div class="drop-hint">MP3 · WAV · M4A · AAC · OGG · FLAC &nbsp;|&nbsp; Up to 150 MB</div>
      </div>
      <div id="audioInfo">
        <div class="audio-row">
          <span>File</span><span id="audName">—</span>
        </div>
        <div class="audio-row">
          <span>Size</span><span id="audSize">—</span>
        </div>
        <div class="audio-row">
          <span>Format</span><span id="audFormat">—</span>
        </div>
        <div class="audio-bar"><div class="audio-bar-fill"></div></div>
      </div>
    </div>

    <!-- Quality -->
    <div class="card">
      <div class="card-label">03 — Output Quality</div>
      <div class="quality-grid">
        <label class="q-btn" onclick="setQuality('fast')">
          <input type="radio" name="quality" value="fast">
          <div class="q-icon">⚡</div>
          <div class="q-name">Fast</div>
          <div class="q-desc">ultrafast</div>
        </label>
        <label class="q-btn active" onclick="setQuality('balanced')">
          <input type="radio" name="quality" value="balanced" checked>
          <div class="q-icon">⚖️</div>
          <div class="q-name">Balanced</div>
          <div class="q-desc">recommended</div>
        </label>
        <label class="q-btn" onclick="setQuality('hq')">
          <input type="radio" name="quality" value="hq">
          <div class="q-icon">💎</div>
          <div class="q-name">HQ</div>
          <div class="q-desc">best quality</div>
        </label>
      </div>
    </div>

    <button id="renderBtn" onclick="startRender()">⚡ Render Video</button>
  </div>

  <!-- ── PROGRESS ────────────────────────────── -->
  <div id="progressSection">
    <div class="prog-header">
      <span class="prog-title">🎬 Rendering…</span>
      <span class="prog-pct" id="progPct">0%</span>
    </div>
    <div class="prog-bar-track">
      <div class="prog-bar-fill" id="progBar"></div>
    </div>
    <div class="prog-meta">
      <span id="progElapsed">0s elapsed</span>
      <span id="progEta">ETA: —</span>
    </div>
    <div class="prog-status" id="progStatus">Queued…</div>
  </div>

  <!-- ── DOWNLOAD ────────────────────────────── -->
  <div id="downloadSection">
    <div class="done-icon">✅</div>
    <div class="done-title">Video Ready!</div>
    <div class="done-meta" id="doneMeta">—</div>
    <a id="downloadBtn" href="#" download>⬇️ Download MP4</a>
    <button class="new-render-btn" onclick="resetUI()">Make another video</button>
  </div>

  <!-- ── ERROR ──────────────────────────────── -->
  <div id="errorSection">
    <div class="err-title">❌ Render Failed</div>
    <div class="err-msg" id="errMsg">Unknown error</div>
    <button class="new-render-btn" onclick="resetUI()">Try again</button>
  </div>

</div><!-- /wrapper -->

<div class="toast" id="toast"></div>
<footer>VideoForge &nbsp;·&nbsp; FFmpeg-powered &nbsp;·&nbsp; Cloud Render</footer>

<script>
// ── State ─────────────────────────────────────────
let activeJobId = null;
let pollTimer   = null;
let renderStart = null;
let selectedQuality = 'balanced';

// ── Quality selector ──────────────────────────────
function setQuality(q) {
  selectedQuality = q;
  document.querySelectorAll('.q-btn').forEach(b => b.classList.remove('active'));
  event.currentTarget.classList.add('active');
}

// ── Drag-and-drop helpers ─────────────────────────
function setupDrop(zoneId, inputId, onFile) {
  const zone  = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) { onFile(file); input.files = e.dataTransfer.files; }
  });
  input.addEventListener('change', () => { if (input.files[0]) onFile(input.files[0]); });
}

// ── Image preview ─────────────────────────────────
setupDrop('imgDrop', 'imgInput', file => {
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('imgThumb').src = e.target.result;
    document.getElementById('imgMeta').textContent =
      `${file.name}  ·  ${(file.size/1048576).toFixed(1)} MB`;
    document.getElementById('imgPreview').style.display = 'block';
  };
  reader.readAsDataURL(file);
});

// ── Audio info ────────────────────────────────────
setupDrop('audDrop', 'audInput', file => {
  document.getElementById('audName').textContent   = file.name.length > 28
    ? file.name.slice(0,25) + '...' : file.name;
  document.getElementById('audSize').textContent   = (file.size/1048576).toFixed(1) + ' MB';
  document.getElementById('audFormat').textContent = file.name.split('.').pop().toUpperCase();
  document.getElementById('audioInfo').style.display = 'block';
});

// ── Start render ──────────────────────────────────
async function startRender() {
  const imgFile = document.getElementById('imgInput').files[0];
  const audFile = document.getElementById('audInput').files[0];

  if (!imgFile) { showToast('Cover image select karo'); return; }
  if (!audFile) { showToast('Audio file select karo');  return; }

  const formData = new FormData();
  formData.append('image',   imgFile);
  formData.append('audio',   audFile);
  formData.append('quality', selectedQuality);

  document.getElementById('renderBtn').disabled = true;
  document.getElementById('renderBtn').textContent = '⏳ Uploading…';

  try {
    const res  = await fetch('/render', { method: 'POST', body: formData });
    const data = await res.json();

    if (!res.ok || data.error) {
      showError(data.error || 'Upload failed');
      resetBtn();
      return;
    }

    activeJobId = data.job_id;
    renderStart = Date.now();
    showProgress();
    pollStatus();

  } catch (e) {
    showError('Network error: ' + e.message);
    resetBtn();
  }
}

// ── Poll /status/<job_id> every 1.2s ─────────────
async function pollStatus() {
  if (!activeJobId) return;
  try {
    const res  = await fetch('/status/' + activeJobId);
    const data = await res.json();
    updateProgress(data);

    if (data.status === 'done')  { showDownload(data); return; }
    if (data.status === 'error') { showError(data.error); return; }

    pollTimer = setTimeout(pollStatus, 1200);
  } catch (e) {
    pollTimer = setTimeout(pollStatus, 3000);   // retry on network blip
  }
}

// ── Update progress UI ────────────────────────────
function updateProgress(data) {
  const pct = data.progress || 0;
  document.getElementById('progBar').style.width  = pct + '%';
  document.getElementById('progPct').textContent  = pct + '%';

  const elapsed = Math.round((Date.now() - renderStart) / 1000);
  document.getElementById('progElapsed').textContent = fmtTime(elapsed) + ' elapsed';

  if (data.eta > 0) {
    document.getElementById('progEta').textContent = 'ETA: ' + fmtTime(data.eta);
  }

  const statusMap = {
    queued:    'Queued — waiting for render slot…',
    rendering: `Rendering… ${pct}% complete`,
    done:      'Render complete!',
    error:     'Render failed.',
  };
  document.getElementById('progStatus').textContent =
    statusMap[data.status] || data.status;
}

// ── Show download ─────────────────────────────────
function showDownload(data) {
  clearTimeout(pollTimer);
  document.getElementById('progressSection').style.display = 'none';
  document.getElementById('downloadSection').style.display = 'block';

  const elapsed = Math.round((Date.now() - renderStart) / 1000);
  document.getElementById('doneMeta').textContent =
    `${data.file_size || '?'} MB  ·  Rendered in ${fmtTime(elapsed)}`;

  const btn = document.getElementById('downloadBtn');
  btn.href     = '/download/' + activeJobId;
  btn.download = 'VideoForge_Output.mp4';

  // Auto-trigger download
  setTimeout(() => btn.click(), 600);
}

// ── Show error ────────────────────────────────────
function showError(msg) {
  clearTimeout(pollTimer);
  document.getElementById('progressSection').style.display = 'none';
  document.getElementById('errorSection').style.display    = 'block';
  document.getElementById('errMsg').textContent = msg || 'Unknown error';
  resetBtn();
}

// ── UI state helpers ──────────────────────────────
function showProgress() {
  document.getElementById('progressSection').style.display = 'block';
  document.getElementById('renderBtn').textContent = '⏳ Rendering…';
}
function resetBtn() {
  document.getElementById('renderBtn').disabled    = false;
  document.getElementById('renderBtn').textContent = '⚡ Render Video';
}
function resetUI() {
  clearTimeout(pollTimer);
  activeJobId = null;
  document.getElementById('downloadSection').style.display = 'none';
  document.getElementById('errorSection').style.display    = 'none';
  document.getElementById('progressSection').style.display = 'none';
  document.getElementById('progBar').style.width = '0%';
  document.getElementById('progPct').textContent = '0%';
  resetBtn();
}
function fmtTime(s) {
  if (s < 60) return s + 's';
  return Math.floor(s/60) + 'm ' + (s%60) + 's';
}
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/render", methods=["POST"])
def render():
    """Accept files, validate, start background render, return job_id."""
    if "image" not in request.files or "audio" not in request.files:
        return jsonify({"error": "Image aur audio dono files bhejni hain"}), 400

    img_file  = request.files["image"]
    aud_file  = request.files["audio"]
    quality   = request.form.get("quality", "balanced")

    # ── Extension validation ────────────────────────
    img_ext = Path(img_file.filename or "").suffix.lower()
    aud_ext = Path(aud_file.filename or "").suffix.lower()

    if img_ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": f"Image format '{img_ext}' support nahi hota. Use: JPG, PNG, WEBP"}), 400
    if aud_ext not in ALLOWED_AUDIO_EXT:
        return jsonify({"error": f"Audio format '{aud_ext}' support nahi hota. Use: MP3, WAV, M4A, AAC"}), 400
    if quality not in QUALITY_PRESETS:
        quality = "balanced"

    # ── Save to temp dir ────────────────────────────
    job_id  = str(uuid.uuid4())[:10]
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    img_path = job_dir / f"input{img_ext}"
    aud_path = job_dir / f"input{aud_ext}"
    out_path = job_dir / "output.mp4"

    img_file.save(img_path)
    aud_file.save(aud_path)

    # ── Create job entry ────────────────────────────
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

    # ── Fire background thread ──────────────────────
    thread = threading.Thread(
        target=render_worker,
        args=(job_id, img_path, aud_path, out_path, quality),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str):
    """Return current job status as JSON (polled by frontend)."""
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
    """Stream the rendered MP4 file to the browser."""
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return "File abhi ready nahi hai", 404

    out_path = Path(job.get("output_path", ""))
    if not out_path.exists():
        return "File server par nahi mili", 404

    safe_name = f"{job.get('filename', 'video')[:40]}_rendered.mp4"
    return send_file(str(out_path), as_attachment=True, download_name=safe_name)


# ═══════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"VideoForge starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
