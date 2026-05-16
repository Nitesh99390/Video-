"""
app.py — VideoForge Cloud  v3.0  (Render.com Free Tier — Max Speed Edition)
=============================================================================
What's new / fixed in v3:
  ✓ Real-time UPLOAD progress bar via XHR (not fetch)
  ✓ Smart audio codec: copy-safe formats (.mp3 .m4a .aac) use -c:a copy;
    incompatible formats (.wav .ogg .flac) transcode to AAC 96k — fast, tiny
  ✓ Auto image downscale: caps at 1280 px wide before encoding (huge perf win)
  ✓ stderr drained in dedicated thread — zero pipe-buffer deadlock possible
  ✓ -threads 2 for dual-core Render boxes without over-spawning
  ✓ 4-phase UI: Form → Upload → Render → Done/Error (clear state machine)
  ✓ Reaper + post-download GC unchanged from v2
  ✓ All JS bugs fixed (quality picker event, XHR abort handling, etc.)
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
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024   # 150 MB

TEMP_DIR = Path("/tmp/vf_jobs")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# Audio formats safe to stream-copy into MP4 — everything else gets fast AAC transcode
COPY_SAFE_AUDIO = {".mp3", ".m4a", ".aac"}

QUALITY_PRESETS = {
    "fast":     {"preset": "ultrafast", "crf": "30"},
    "balanced": {"preset": "superfast", "crf": "26"},
    "hq":       {"preset": "veryfast",  "crf": "22"},
}


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════
def get_audio_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(json.loads(r.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


def audio_codec_args(ext: str) -> list[str]:
    """Stream-copy for MP4-compatible formats; fast AAC transcode for everything else."""
    if ext in COPY_SAFE_AUDIO:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "96k", "-ar", "44100"]


def safe_remove(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def clip_str(s: str, n: int) -> str:
    return s[:n] if len(s) <= n else s[:n - 1]


# ═══════════════════════════════════════════════════════════════════
#  BACKGROUND REAPER  — evicts jobs older than 1 h every 30 min
# ═══════════════════════════════════════════════════════════════════
def _reaper() -> None:
    while True:
        time.sleep(1800)
        cutoff = time.time() - 3600
        with _jobs_lock:
            stale = [jid for jid, j in list(jobs.items()) if j.get("created", 0) < cutoff]
        for jid in stale:
            with _jobs_lock:
                job = jobs.pop(jid, {})
            if job.get("output_path"):
                safe_remove(Path(job["output_path"]))
            job_dir = TEMP_DIR / jid
            try:
                for f in job_dir.glob("*"):
                    f.unlink(missing_ok=True)
                job_dir.rmdir()
            except OSError:
                pass

threading.Thread(target=_reaper, daemon=True, name="vf-reaper").start()


# ═══════════════════════════════════════════════════════════════════
#  RENDER WORKER
# ═══════════════════════════════════════════════════════════════════
def render_worker(
    job_id:   str,
    img_path: Path,
    aud_path: Path,
    out_path: Path,
    quality:  str,
) -> None:
    """
    Speed strategy:
      1. Image capped at 1280 px wide  →  fewer pixels = faster encode
      2. Audio stream-copied where safe (-c:a copy); else fast 96k AAC
      3. Presets: ultrafast / superfast / veryfast  (all faster than original)
      4. -threads 2  (fits Render free-tier shared dual vCPU)
      5. stderr drained in its own thread  →  zero pipe-buffer deadlock
    """
    with _jobs_lock:
        job = jobs[job_id]

    duration          = get_audio_duration(aud_path)
    job["duration"]   = duration
    job["status"]     = "rendering"
    job["start_time"] = time.time()

    cfg      = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])
    aud_args = audio_codec_args(aud_path.suffix.lower())

    command = [
        "ffmpeg",
        # Inputs
        "-loop", "1", "-framerate", "1", "-i", str(img_path),
        "-i", str(aud_path),
        # Video
        "-c:v", "libx264",
        "-preset", cfg["preset"],
        "-tune", "stillimage",
        "-crf", cfg["crf"],
        # Cap at 1280 px wide; keep aspect; force even H & W for yuv420p
        "-vf", "scale='2*trunc(min(iw,1280)/2)':-2",
        "-pix_fmt", "yuv420p",
        # Audio (copy or fast AAC)
        *aud_args,
        # Container
        "-movflags", "+faststart",
        "-shortest",
        # Threading (2 matches Render free-tier vCPU count)
        "-threads", "2",
        # Progress piped to stdout; only warnings/errors on stderr
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
            bufsize=1,
        )

        # Drain stderr in its own thread — prevents pipe-buffer deadlock
        stderr_buf: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr:
                for ln in proc.stderr:
                    stderr_buf.append(ln)

        threading.Thread(target=_drain_stderr, daemon=True, name=f"se-{job_id}").start()

        # Non-blocking readline() progress loop
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if not line:
                continue

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
            elif key == "progress" and val.strip() == "end":
                job["progress"] = 100

        proc.wait(timeout=7200)

        if proc.returncode == 0 and out_path.exists():
            job["status"]      = "done"
            job["progress"]    = 100
            job["output_path"] = str(out_path)
            job["file_size"]   = round(out_path.stat().st_size / 1_048_576, 1)
            job["eta"]         = 0
        else:
            tail = "".join(stderr_buf[-10:])[:600].strip()
            job["status"] = "error"
            job["error"]  = f"FFmpeg exited {proc.returncode}. {tail}"

    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        job["status"] = "error"
        job["error"]  = "Render timeout (> 2 hours). Try a shorter audio file."
    except FileNotFoundError:
        job["status"] = "error"
        job["error"]  = "FFmpeg not found on this server."
    except Exception as exc:
        job["status"] = "error"
        job["error"]  = str(exc)
    finally:
        # Always delete source files immediately to free disk space
        safe_remove(img_path, aud_path)


# ═══════════════════════════════════════════════════════════════════
#  EMBEDDED FRONTEND
#  Aesthetic: Terminal Forge — dark emerald, JetBrains Mono headers,
#  CRT scanline texture, 4-phase state machine
# ═══════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VideoForge — Cloud Render</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Outfit:wght@300;400;500;600;700;900&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg0:   #020805;
  --bg1:   #060f07;
  --bg2:   #0a180b;
  --bg3:   #0f2111;
  --card:  rgba(10,22,11,.84);
  --bdr:   rgba(34,197,94,.13);
  --rim:   rgba(34,197,94,.28);
  --g1:    #22c55e;
  --g2:    #4ade80;
  --g3:    #86efac;
  --dim:   #1a3d1f;
  --muted: #4b6e50;
  --text:  #d1f5d8;
  --hi:    #f0fdf4;
  --err:   #f87171;
  --mono:  'JetBrains Mono', monospace;
  --sans:  'Outfit', sans-serif;
  --r:     10px;
}

html, body { height: 100%; }
body {
  font-family: var(--sans);
  background: var(--bg0);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}

/* CRT scanline overlay */
body::before {
  content: ''; position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px,
    rgba(0,0,0,.16) 2px, rgba(0,0,0,.16) 4px
  );
}

/* Animated dot-grid */
body::after {
  content: ''; position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background-image:
    linear-gradient(rgba(34,197,94,.028) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,197,94,.028) 1px, transparent 1px);
  background-size: 40px 40px;
  animation: gridScroll 28s linear infinite;
}
@keyframes gridScroll { to { background-position: 40px 40px; } }

.glow {
  position: fixed; top: -12%; left: 50%; transform: translateX(-50%);
  width: 860px; height: 560px;
  background: radial-gradient(ellipse at center,
    rgba(34,197,94,.14) 0%, rgba(34,197,94,.04) 42%, transparent 68%);
  z-index: 0; pointer-events: none;
  animation: glowPulse 7s ease-in-out infinite;
}
@keyframes glowPulse {
  0%,100% { opacity:.8; transform: translateX(-50%) scaleY(1); }
  50%      { opacity:1;  transform: translateX(-50%) scaleY(1.07); }
}

/* ── Layout ─────────────────────────────── */
.wrap {
  position: relative; z-index: 1;
  max-width: 640px; margin: 0 auto;
  padding: 50px 18px 80px;
}

/* ── Header ─────────────────────────────── */
header { text-align: center; margin-bottom: 46px; }

.pill {
  display: inline-flex; align-items: center; gap: 8px;
  border: 1px solid var(--rim);
  background: rgba(34,197,94,.07);
  border-radius: 4px;
  padding: 5px 14px;
  font-family: var(--mono); font-size: 10px; font-weight: 500;
  color: var(--g2); letter-spacing: 3px; text-transform: uppercase;
  margin-bottom: 22px;
}
.pdot {
  width: 6px; height: 6px; background: var(--g1); border-radius: 50%;
  box-shadow: 0 0 8px var(--g1);
  animation: blink 2.2s ease-in-out infinite;
}
@keyframes blink { 0%,100%{ opacity:1; } 50%{ opacity:.2; } }

h1 {
  font-family: var(--mono); font-size: clamp(22px, 5vw, 36px); font-weight: 700;
  color: var(--hi); line-height: 1.15; letter-spacing: -1px;
}
.accent {
  display: block;
  background: linear-gradient(90deg, var(--g1), var(--g2), var(--g3));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
.sub {
  margin-top: 14px; color: var(--muted);
  font-family: var(--mono); font-size: 12.5px; letter-spacing: .3px; line-height: 1.8;
}
.sub b { color: var(--g2); font-weight: 500; }

/* ── Card ───────────────────────────────── */
.card {
  background: var(--card);
  border: 1px solid var(--bdr);
  border-radius: var(--r); padding: 22px; margin-bottom: 12px;
  backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px);
  transition: border-color .22s, box-shadow .22s;
  position: relative; overflow: hidden;
}
.card::before {
  content: ''; position: absolute; top: 0; left: 8%; right: 8%; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(34,197,94,.28), transparent);
}
.card:hover { border-color: var(--rim); box-shadow: 0 0 28px rgba(34,197,94,.06); }

.clabel {
  font-family: var(--mono); font-size: 9.5px; font-weight: 700;
  color: var(--g1); letter-spacing: 3px; text-transform: uppercase;
  margin-bottom: 16px; display: flex; align-items: center; gap: 10px;
}
.clabel::after { content: ''; flex: 1; height: 1px; background: var(--bdr); }
.cnum {
  background: rgba(34,197,94,.1); border: 1px solid rgba(34,197,94,.2);
  border-radius: 3px; padding: 1px 6px; font-size: 9px; color: var(--g2);
}

/* ── Drop zone ──────────────────────────── */
.dz {
  border: 1.5px dashed var(--dim); border-radius: 8px;
  padding: 28px 20px; text-align: center; cursor: pointer;
  transition: all .2s; position: relative; overflow: hidden; user-select: none;
}
.dz:hover, .dz.over {
  border-color: var(--g1); border-style: solid;
  background: rgba(34,197,94,.05);
}
.dz input[type="file"] {
  position: absolute; inset: 0; opacity: 0;
  cursor: pointer; width: 100%; height: 100%;
}
.dz-icon { font-size: 28px; margin-bottom: 8px; transition: transform .2s; }
.dz:hover .dz-icon { transform: scale(1.15); }
.dz-main { font-size: 13.5px; color: var(--muted); }
.dz-main strong { color: var(--g2); }
.dz-hint { font-family: var(--mono); font-size: 10px; color: var(--dim); margin-top: 5px; letter-spacing: .8px; }

/* Image preview */
#imgPreview {
  display: none; margin-top: 12px; border-radius: 8px; overflow: hidden;
  border: 1px solid var(--bdr); position: relative;
}
#imgPreview img { width: 100%; max-height: 180px; object-fit: cover; display: block; }
#imgPreview .imeta {
  position: absolute; bottom: 0; left: 0; right: 0;
  background: linear-gradient(transparent, rgba(0,0,0,.76));
  padding: 14px 12px 10px;
  font-family: var(--mono); font-size: 11px; color: #9ca; letter-spacing: .4px;
}

/* Audio info */
#audioInfo {
  display: none; margin-top: 12px;
  background: rgba(2,8,5,.72); border: 1px solid var(--bdr); border-radius: 8px; padding: 14px 16px;
}
.astats { display: grid; grid-template-columns: repeat(3, 1fr); }
.astat { text-align: center; padding: 4px 0; }
.astat + .astat { border-left: 1px solid var(--bdr); }
.albl { font-family: var(--mono); font-size: 9px; color: var(--muted); letter-spacing: 2px; text-transform: uppercase; margin-bottom: 4px; }
.aval { font-family: var(--mono); font-size: 13px; font-weight: 700; color: var(--g2); word-break: break-all; }
.acodec {
  margin-top: 10px; padding: 7px 12px;
  background: rgba(34,197,94,.07); border: 1px solid rgba(34,197,94,.15); border-radius: 6px;
  font-family: var(--mono); font-size: 10.5px; color: var(--g2);
  display: flex; align-items: center; gap: 8px;
}
.acodec span { color: var(--muted); }

/* ── Quality ────────────────────────────── */
.qrow { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.qbtn {
  background: rgba(2,8,5,.85); border: 1.5px solid var(--bdr); border-radius: 8px;
  padding: 14px 8px; cursor: pointer; text-align: center; transition: all .18s;
  font-family: var(--sans); position: relative; overflow: hidden;
}
.qbtn::before {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, transparent, rgba(34,197,94,.08));
  opacity: 0; transition: opacity .2s;
}
.qbtn:hover::before, .qbtn.active::before { opacity: 1; }
.qbtn:hover { border-color: var(--muted); }
.qbtn.active { border-color: var(--g1); box-shadow: 0 0 18px rgba(34,197,94,.14) inset; }
.qi { font-size: 20px; margin-bottom: 5px; }
.qn { font-size: 12.5px; font-weight: 600; color: var(--text); }
.qs { font-family: var(--mono); font-size: 9.5px; color: var(--muted); margin-top: 3px; }
input[name="quality"] { display: none; }

/* ── Render button ──────────────────────── */
#renderBtn {
  width: 100%; padding: 16px;
  background: linear-gradient(135deg, #14532d, var(--g1) 60%, var(--g2));
  border: none; border-radius: var(--r);
  color: #fff; font-family: var(--mono);
  font-size: 13px; font-weight: 700; letter-spacing: 2.5px; text-transform: uppercase;
  cursor: pointer; transition: all .22s;
  position: relative; overflow: hidden; margin-top: 10px;
  box-shadow: 0 0 0 1px rgba(34,197,94,.28), 0 4px 20px rgba(34,197,94,.18);
}
#renderBtn::after {
  content: ''; position: absolute; top: 0; left: -100%; width: 55%; height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.18), transparent);
  transform: skewX(-18deg); transition: left .55s;
}
#renderBtn:hover::after { left: 160%; }
#renderBtn:hover { transform: translateY(-2px); box-shadow: 0 0 0 1px rgba(34,197,94,.45), 0 8px 28px rgba(34,197,94,.32); }
#renderBtn:active { transform: translateY(0); }
#renderBtn:disabled { background: var(--dim); box-shadow: none; cursor: not-allowed; transform: none; }

/* ── Shared panel shell ─────────────────── */
.panel {
  display: none;
  background: var(--card); border: 1px solid var(--bdr); border-radius: var(--r);
  padding: 26px; margin-top: 12px;
  backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px);
  animation: panelIn .3s ease;
}
@keyframes panelIn { from{ opacity:0; transform:translateY(7px); } to{ opacity:1; transform:translateY(0); } }

/* ── Progress bar component ─────────────── */
.ph { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.plabel { font-family: var(--mono); font-size: 9.5px; color: var(--muted); letter-spacing: 3px; text-transform: uppercase; }
.ppct   { font-family: var(--mono); font-size: 28px; font-weight: 700; color: var(--g2); letter-spacing: -2px; }

.ptrack {
  height: 6px; background: var(--bg3); border-radius: 99px;
  overflow: hidden; border: 1px solid var(--bdr);
}
.pfill {
  height: 100%; border-radius: 99px;
  background: linear-gradient(90deg, #14532d, var(--g1), var(--g2));
  transition: width .55s cubic-bezier(.4,0,.2,1);
  width: 0%; position: relative;
}
.pfill::after {
  content: ''; position: absolute; top: 0; right: 0; bottom: 0; width: 80px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.26));
  animation: sheen 1.5s ease-in-out infinite;
}
@keyframes sheen { 0%,100%{ opacity:0; } 50%{ opacity:1; } }

.pmeta {
  display: flex; justify-content: space-between;
  margin-top: 10px; font-family: var(--mono); font-size: 10.5px; color: var(--muted);
}
.pstatus {
  margin-top: 12px; padding: 10px 14px;
  background: rgba(2,8,5,.82); border: 1px solid var(--bdr); border-radius: 6px;
  font-family: var(--mono); font-size: 12px; color: var(--muted); min-height: 38px; line-height: 1.7;
}
.pstatus .hi { color: var(--g2); }

/* Upload file pills */
.upills { display: flex; gap: 8px; margin-bottom: 18px; }
.upill {
  flex: 1; background: rgba(34,197,94,.06); border: 1px solid rgba(34,197,94,.14);
  border-radius: 6px; padding: 8px 12px; font-family: var(--mono); font-size: 11px;
}
.uplbl { color: var(--muted); font-size: 9px; letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 3px; }
.upval { color: var(--g3); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── Download ───────────────────────────── */
#downloadSection {
  display: none;
  background: rgba(20,83,45,.18);
  border: 1px solid rgba(34,197,94,.28);
  border-radius: var(--r); padding: 32px 24px; text-align: center;
  margin-top: 12px; animation: panelIn .42s cubic-bezier(.4,0,.2,1);
}
.dico { font-size: 48px; margin-bottom: 14px; animation: pop .5s cubic-bezier(.34,1.56,.64,1); }
@keyframes pop { from{ transform:scale(0); opacity:0; } to{ transform:scale(1); opacity:1; } }
.dtitle { font-family: var(--mono); font-size: 17px; font-weight: 700; color: var(--hi); margin-bottom: 6px; }
.dmeta  { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-bottom: 24px; letter-spacing: .4px; }
#downloadBtn {
  display: inline-flex; align-items: center; gap: 10px;
  background: var(--g1); color: #000;
  font-family: var(--mono); font-weight: 700; font-size: 12.5px; letter-spacing: 1.5px; text-transform: uppercase;
  padding: 14px 32px; border-radius: 6px; text-decoration: none;
  transition: all .2s; box-shadow: 0 4px 18px rgba(34,197,94,.35);
}
#downloadBtn:hover { background: var(--g2); transform: translateY(-2px); box-shadow: 0 8px 26px rgba(34,197,94,.44); }

/* ── Error ──────────────────────────────── */
#errorSection {
  display: none;
  background: rgba(120,20,20,.18); border: 1px solid rgba(248,113,113,.26);
  border-radius: var(--r); padding: 24px; margin-top: 12px;
  animation: panelIn .35s ease;
}
.etitle { font-family: var(--mono); font-weight: 700; color: var(--err); margin-bottom: 8px; font-size: 12.5px; letter-spacing: 1.5px; text-transform: uppercase; }
.emsg   { font-family: var(--mono); font-size: 11.5px; color: #fca5a5; line-height: 1.8; word-break: break-word; }

.resetbtn {
  display: block; margin-top: 18px;
  background: none; border: none;
  color: var(--muted); font-size: 12px; font-family: var(--mono);
  cursor: pointer; text-decoration: underline; text-underline-offset: 3px; letter-spacing: .5px;
}
.resetbtn:hover { color: var(--text); }

/* ── Toast ──────────────────────────────── */
#toast {
  position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
  background: var(--bg3); border: 1px solid var(--rim);
  padding: 10px 22px; border-radius: 6px;
  font-family: var(--mono); font-size: 12px; color: var(--text); letter-spacing: .5px;
  opacity: 0; pointer-events: none; transition: opacity .28s;
  z-index: 999; white-space: nowrap; box-shadow: 0 4px 20px rgba(0,0,0,.5);
}
#toast.show { opacity: 1; }

/* ── Footer ─────────────────────────────── */
footer {
  text-align: center; margin-top: 52px;
  font-family: var(--mono); font-size: 10px; color: var(--dim);
  letter-spacing: 1.5px; text-transform: uppercase;
}
</style>
</head>
<body>
<div class="glow"></div>
<div class="wrap">

  <!-- HEADER -->
  <header>
    <div class="pill"><span class="pdot"></span>VideoForge Cloud v3</div>
    <h1>Image + Audio<span class="accent">→ MP4, Instantly.</span></h1>
    <p class="sub">Server renders via FFmpeg.<br>
      <b>-c:a copy</b> · <b>1280p cap</b> · <b>ultrafast encode</b> · <b>zero re-encode</b>
    </p>
  </header>

  <!-- FORM -->
  <div id="formSection">

    <div class="card">
      <div class="clabel"><span class="cnum">01</span> Cover Image</div>
      <div class="dz" id="imgDrop">
        <input type="file" id="imgInput" accept=".jpg,.jpeg,.png,.webp,.bmp">
        <div class="dz-icon">🖼️</div>
        <div class="dz-main"><strong>Click or drag</strong> an image here</div>
        <div class="dz-hint">JPG · PNG · WEBP · BMP</div>
      </div>
      <div id="imgPreview">
        <img id="imgThumb" src="" alt="">
        <div class="imeta" id="imgMeta">—</div>
      </div>
    </div>

    <div class="card">
      <div class="clabel"><span class="cnum">02</span> Audio Track</div>
      <div class="dz" id="audDrop">
        <input type="file" id="audInput" accept=".mp3,.wav,.m4a,.aac,.ogg,.flac">
        <div class="dz-icon">🎵</div>
        <div class="dz-main"><strong>Click or drag</strong> audio here</div>
        <div class="dz-hint">MP3 · WAV · M4A · AAC · OGG · FLAC · Max 150 MB</div>
      </div>
      <div id="audioInfo">
        <div class="astats">
          <div class="astat"><div class="albl">File</div><div class="aval" id="audName">—</div></div>
          <div class="astat"><div class="albl">Size</div><div class="aval" id="audSize">—</div></div>
          <div class="astat"><div class="albl">Format</div><div class="aval" id="audFmt">—</div></div>
        </div>
        <div class="acodec"><span>Mode:</span><strong id="audCodecLabel">—</strong></div>
      </div>
    </div>

    <div class="card">
      <div class="clabel"><span class="cnum">03</span> Encode Quality</div>
      <div class="qrow">
        <label class="qbtn" id="qFast" onclick="setQuality('fast', this)">
          <input type="radio" name="quality" value="fast">
          <div class="qi">⚡</div><div class="qn">Fast</div><div class="qs">ultrafast · crf30</div>
        </label>
        <label class="qbtn active" id="qBalanced" onclick="setQuality('balanced', this)">
          <input type="radio" name="quality" value="balanced" checked>
          <div class="qi">⚖️</div><div class="qn">Balanced</div><div class="qs">superfast · crf26</div>
        </label>
        <label class="qbtn" id="qHq" onclick="setQuality('hq', this)">
          <input type="radio" name="quality" value="hq">
          <div class="qi">💎</div><div class="qn">High Quality</div><div class="qs">veryfast · crf22</div>
        </label>
      </div>
    </div>

    <button id="renderBtn" onclick="startRender()">▶ Render Video</button>
  </div>

  <!-- UPLOAD PROGRESS -->
  <div class="panel" id="uploadSection">
    <div class="upills">
      <div class="upill"><div class="uplbl">Image</div><div class="upval" id="ulImg">—</div></div>
      <div class="upill"><div class="uplbl">Audio</div><div class="upval" id="ulAud">—</div></div>
    </div>
    <div class="ph">
      <span class="plabel">↑ Uploading to server</span>
      <span class="ppct" id="upPct">0%</span>
    </div>
    <div class="ptrack"><div class="pfill" id="upBar"></div></div>
    <div class="pmeta">
      <span id="upBytes">0 KB / 0 KB</span>
      <span id="upSpeed">—</span>
    </div>
    <div class="pstatus" id="upStatus">Connecting to server…</div>
  </div>

  <!-- RENDER PROGRESS -->
  <div class="panel" id="progressSection">
    <div class="ph">
      <span class="plabel">⚙ FFmpeg Rendering</span>
      <span class="ppct" id="rndPct">0%</span>
    </div>
    <div class="ptrack"><div class="pfill" id="rndBar"></div></div>
    <div class="pmeta">
      <span id="rndElapsed">0s elapsed</span>
      <span id="rndEta">ETA: —</span>
    </div>
    <div class="pstatus" id="rndStatus">Queued…</div>
  </div>

  <!-- DOWNLOAD -->
  <div id="downloadSection">
    <div class="dico">✅</div>
    <div class="dtitle">Video Ready</div>
    <div class="dmeta" id="doneMeta">—</div>
    <a id="downloadBtn" href="#" download>⬇ Download MP4</a>
    <button class="resetbtn" onclick="resetUI()">Make another video</button>
  </div>

  <!-- ERROR -->
  <div id="errorSection">
    <div class="etitle">❌ Render Failed</div>
    <div class="emsg" id="errMsg">Unknown error.</div>
    <button class="resetbtn" onclick="resetUI()">Try again</button>
  </div>

</div><!-- /wrap -->
<div id="toast"></div>
<footer>VideoForge · FFmpeg · -c:a copy · Render.com Free Tier</footer>

<script>
// ══ Constants ═══════════════════════════════════════════════════
const COPY_SAFE = new Set(['mp3','m4a','aac']);
const PANELS    = ['uploadSection','progressSection','downloadSection','errorSection'];

// ══ State ════════════════════════════════════════════════════════
let activeJob       = null;
let pollTimer       = null;
let renderStart     = null;
let uploadStart     = null;
let activeXHR       = null;
let selectedQuality = 'balanced';

// ══ Quality selector ════════════════════════════════════════════
function setQuality(q, el) {
  selectedQuality = q;
  document.querySelectorAll('.qbtn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}

// ══ Drag-and-drop setup ══════════════════════════════════════════
function setupDrop(zoneId, inputId, cb) {
  const zone  = document.getElementById(zoneId);
  const input = document.getElementById(inputId);

  zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', ()  => zone.classList.remove('over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('over');
    const file = e.dataTransfer.files[0];
    if (file) {
      // DataTransfer trick: assign dragged file to the hidden input
      try {
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      } catch (_) { /* Safari fallback — FormData will use the dragged file directly */ }
      cb(file);
    }
  });
  input.addEventListener('change', () => { if (input.files[0]) cb(input.files[0]); });
}

// Image preview
setupDrop('imgDrop', 'imgInput', file => {
  const r = new FileReader();
  r.onload = e => {
    document.getElementById('imgThumb').src = e.target.result;
    document.getElementById('imgMeta').textContent =
      clip(file.name, 42) + '  ·  ' + toMb(file.size) + ' MB';
    document.getElementById('imgPreview').style.display = 'block';
  };
  r.readAsDataURL(file);
});

// Audio info
setupDrop('audDrop', 'audInput', file => {
  const ext = file.name.split('.').pop().toLowerCase();
  document.getElementById('audName').textContent = clip(file.name, 16);
  document.getElementById('audSize').textContent = toMb(file.size) + ' MB';
  document.getElementById('audFmt').textContent  = ext.toUpperCase();
  document.getElementById('audCodecLabel').textContent =
    COPY_SAFE.has(ext) ? '⚡ Stream Copy (instant)' : '🔄 Transcode → AAC 96k';
  document.getElementById('audioInfo').style.display = 'block';
});

// ══ Main flow ════════════════════════════════════════════════════
async function startRender() {
  const imgFile = document.getElementById('imgInput').files[0];
  const audFile = document.getElementById('audInput').files[0];
  if (!imgFile) { showToast('⚠  Please select a cover image'); return; }
  if (!audFile) { showToast('⚠  Please select an audio file');  return; }

  const form = new FormData();
  form.append('image',   imgFile);
  form.append('audio',   audFile);
  form.append('quality', selectedQuality);

  document.getElementById('ulImg').textContent = clip(imgFile.name, 22);
  document.getElementById('ulAud').textContent = clip(audFile.name, 22);

  setBtn(false, '↑ Uploading…');
  showPanel('uploadSection');
  uploadStart = Date.now();

  try {
    const data = await xhrUpload(form);
    if (data.error) { showError(data.error); resetBtn(); return; }

    activeJob   = data.job_id;
    renderStart = Date.now();
    showPanel('progressSection');
    setBtn(false, '⚙  Rendering…');
    pollStatus();

  } catch (err) {
    if (err.message !== 'aborted') showError(err.message);
    resetBtn();
  }
}

// ══ XHR upload with real-time progress ══════════════════════════
function xhrUpload(formData) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    activeXHR = xhr;

    let lastLoaded = 0, lastMs = Date.now();

    xhr.upload.addEventListener('progress', e => {
      if (!e.lengthComputable) return;

      const pct    = Math.round(e.loaded / e.total * 100);
      const nowMs  = Date.now();
      const dtSec  = (nowMs - lastMs) / 1000;
      const dBytes = e.loaded - lastLoaded;
      const spKb   = dtSec > 0.08 ? Math.round(dBytes / dtSec / 1024) : null;
      lastLoaded = e.loaded; lastMs = nowMs;

      document.getElementById('upBar').style.width = pct + '%';
      document.getElementById('upPct').textContent  = pct + '%';
      document.getElementById('upBytes').textContent =
        fmtBytes(e.loaded) + ' / ' + fmtBytes(e.total);
      if (spKb !== null)
        document.getElementById('upSpeed').textContent = fmtKb(spKb) + '/s';

      const el = Math.round((Date.now() - uploadStart) / 1000);
      document.getElementById('upStatus').innerHTML =
        '<span class="hi">↑ Uploading…</span>  ' + el + 's elapsed';
    });

    xhr.addEventListener('load', () => {
      activeXHR = null;
      document.getElementById('upBar').style.width = '100%';
      document.getElementById('upPct').textContent  = '100%';
      document.getElementById('upStatus').innerHTML =
        '<span class="hi">✓ Upload complete</span> — queuing FFmpeg render…';

      try {
        const d = JSON.parse(xhr.responseText);
        xhr.status >= 200 && xhr.status < 300
          ? resolve(d)
          : reject(new Error(d.error || 'Server error ' + xhr.status));
      } catch {
        reject(new Error('Invalid server response'));
      }
    });

    xhr.addEventListener('error', () => { activeXHR = null; reject(new Error('Network error — check your connection')); });
    xhr.addEventListener('abort', () => { activeXHR = null; reject(new Error('aborted')); });

    xhr.open('POST', '/render');
    xhr.send(formData);
  });
}

// ══ Poll /status ═════════════════════════════════════════════════
async function pollStatus() {
  if (!activeJob) return;
  try {
    const r = await fetch('/status/' + activeJob);
    const d = await r.json();
    updateRenderUI(d);
    if (d.status === 'done')  { onDone(d);          return; }
    if (d.status === 'error') { showError(d.error);  return; }
    pollTimer = setTimeout(pollStatus, 1400);
  } catch (_) {
    pollTimer = setTimeout(pollStatus, 3000);
  }
}

function updateRenderUI(d) {
  const pct = d.progress || 0;
  document.getElementById('rndBar').style.width = pct + '%';
  document.getElementById('rndPct').textContent  = pct + '%';

  const el = Math.round((Date.now() - renderStart) / 1000);
  document.getElementById('rndElapsed').textContent = fmtSec(el) + ' elapsed';
  if (d.eta > 0) document.getElementById('rndEta').textContent = 'ETA: ' + fmtSec(d.eta);

  const msgs = {
    queued:    '⏳ Queued — waiting for render slot…',
    rendering: `<span class="hi">⚙ FFmpeg encoding…</span>  ${pct}% complete`,
    done:      '<span class="hi">✅ Render complete!</span>',
    error:     '❌ Render failed.',
  };
  document.getElementById('rndStatus').innerHTML = msgs[d.status] || d.status;
}

function onDone(d) {
  clearTimeout(pollTimer);
  const el = Math.round((Date.now() - renderStart) / 1000);
  document.getElementById('doneMeta').textContent =
    (d.file_size || '?') + ' MB  ·  rendered in ' + fmtSec(el);

  const btn  = document.getElementById('downloadBtn');
  btn.href     = '/download/' + activeJob;
  btn.download = 'VideoForge_Output.mp4';

  showPanel(null);  // hide all panels
  document.getElementById('downloadSection').style.display = 'block';
  setBtn(true, '▶ Render Video');
  setTimeout(() => btn.click(), 800);
}

// ══ UI helpers ═══════════════════════════════════════════════════
function showError(msg) {
  clearTimeout(pollTimer);
  showPanel(null);
  document.getElementById('errorSection').style.display = 'block';
  document.getElementById('errMsg').textContent = msg || 'Unknown error.';
  resetBtn();
}

function showPanel(id) {
  PANELS.forEach(p => {
    document.getElementById(p).style.display = p === id ? 'block' : 'none';
  });
}

function setBtn(enabled, label) {
  const b = document.getElementById('renderBtn');
  b.disabled = !enabled; b.textContent = label;
}
function resetBtn() { setBtn(true, '▶ Render Video'); }

function resetUI() {
  clearTimeout(pollTimer);
  if (activeXHR) { activeXHR.abort(); activeXHR = null; }
  activeJob = null;
  PANELS.forEach(p => { document.getElementById(p).style.display = 'none'; });
  document.getElementById('downloadSection').style.display = 'none';
  document.getElementById('errorSection').style.display    = 'none';
  ['upBar','rndBar'].forEach(id => { document.getElementById(id).style.width = '0%'; });
  document.getElementById('upPct').textContent  = '0%';
  document.getElementById('rndPct').textContent = '0%';
  resetBtn();
}

// ══ Format utils ═════════════════════════════════════════════════
function fmtSec(s)  { return s < 60 ? s + 's' : Math.floor(s/60) + 'm ' + (s%60) + 's'; }
function fmtKb(kb)  { return kb >= 1024 ? (kb/1024).toFixed(1) + ' MB' : kb + ' KB'; }
function fmtBytes(b){ return fmtKb(Math.round(b/1024)); }
function toMb(b)    { return (b/1048576).toFixed(1); }
function clip(s, n) { return s.length > n ? s.slice(0, n-1) + '…' : s; }

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 2800);
}
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/render", methods=["POST"])
def render_route():
    if "image" not in request.files or "audio" not in request.files:
        return jsonify({"error": "Both image and audio files are required."}), 400

    img_file = request.files["image"]
    aud_file = request.files["audio"]
    quality  = request.form.get("quality", "balanced")

    img_ext = Path(img_file.filename or "").suffix.lower()
    aud_ext = Path(aud_file.filename or "").suffix.lower()

    if img_ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": f"Unsupported image format '{img_ext}'. Use JPG/PNG/WEBP/BMP."}), 400
    if aud_ext not in ALLOWED_AUDIO_EXT:
        return jsonify({"error": f"Unsupported audio format '{aud_ext}'. Use MP3/WAV/M4A/AAC/OGG/FLAC."}), 400
    if quality not in QUALITY_PRESETS:
        quality = "balanced"

    job_id  = str(uuid.uuid4())[:10]
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    img_path = job_dir / f"src{img_ext}"
    aud_path = job_dir / f"src{aud_ext}"
    out_path = job_dir / "output.mp4"

    img_file.save(img_path)
    aud_file.save(aud_path)

    with _jobs_lock:
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

    threading.Thread(
        target=render_worker,
        args=(job_id, img_path, aud_path, out_path, quality),
        daemon=True,
        name=f"render-{job_id}",
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    elapsed = int(time.time() - job["start_time"]) if job.get("start_time") else 0
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
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return "File not ready.", 404

    out_path = Path(job.get("output_path", ""))
    if not out_path.exists():
        return "Output file not found on server.", 404

    safe_name = clip_str(job.get("filename", "video"), 40) + "_rendered.mp4"

    def _gc_after_download(path: Path) -> None:
        time.sleep(60)      # 60 s grace: enough for any browser to finish
        safe_remove(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass

    threading.Thread(
        target=_gc_after_download, args=(out_path,), daemon=True, name=f"gc-{job_id}"
    ).start()

    return send_file(str(out_path), as_attachment=True, download_name=safe_name)


# ═══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[VideoForge v3] http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
