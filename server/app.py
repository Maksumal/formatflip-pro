#!/usr/bin/env python3
"""FormatFlip: local-first FFmpeg converter with short-lived file storage."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "assets"
RUNTIME_DIR = PROJECT_ROOT / "server" / "runtime"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
OUTPUT_DIR = RUNTIME_DIR / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Temporary development host: protect local testing from large/concurrent uploads.
# The threaded Waitress adapter starts lazily per request/thread; this process-local
# counter is only for one local instance, not a cross-worker production quota.

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_CONCURRENT_JOBS = 2
FILE_TTL_SECONDS = 60 * 60
CONVERSION_TIMEOUT_SECONDS = 5 * 60
FFMPEG = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
ALLOWED_INPUTS = {"mp4", "mov", "avi", "mkv", "webm", "mp3", "wav", "aac", "flac", "m4a", "ogg", "opus"}

AUDIO_PRESETS: dict[str, list[str]] = {
    "mp3": ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"],
    "wav": ["-vn", "-c:a", "pcm_s16le"],
    "aac": ["-vn", "-c:a", "aac", "-b:a", "192k", "-f", "adts"],
    "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "5"],
    "opus": ["-vn", "-c:a", "libopus", "-b:a", "160k"],
    "flac": ["-vn", "-c:a", "flac", "-compression_level", "5"],
}
VIDEO_PRESETS: dict[str, list[str]] = {
    "mp4": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "webm": ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4", "-crf", "32", "-b:v", "0", "-c:a", "libopus", "-b:a", "128k"],
    "mkv": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-f", "matroska"],
    "mov": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-f", "mov"],
    "avi": ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "libmp3lame", "-b:a", "160k"],
}
FORMATS = {**AUDIO_PRESETS, **VIDEO_PRESETS}
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.logger.setLevel(logging.INFO)
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.RLock()
active_jobs = 0
active_jobs_lock = threading.Lock()


def _find_ffmpeg() -> str | None:
    candidate = FFMPEG
    if candidate and Path(candidate).is_file():
        return candidate
    return shutil.which("ffmpeg")


def _safe_error(text: str, limit: int = 240) -> str:
    cleaned = re.sub(r"[A-Z]:\\[^\s:'\"]+", "[file]", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return (lines[-1] if lines else "Conversion failed")[:limit]


def _consume_pipe(pipe, tail: bytearray, limit: int = 2048) -> None:
    """Drain subprocess output without buffering unbounded FFmpeg logs."""
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[:-limit]
    finally:
        pipe.close()


def _run_conversion(source: Path, output: Path, fmt: str) -> tuple[bool, str]:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return False, "FFmpeg is unavailable on this server."
    codec_args = FORMATS.get(fmt)
    if not codec_args:
        return False, "Unsupported output format."
    if fmt in AUDIO_PRESETS:
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-protocol_whitelist", "file,pipe,crypto,data", "-y", "-i", str(source), "-map", "0:a:0", *codec_args, str(output)]
    else:
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-protocol_whitelist", "file,pipe,crypto,data", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", *codec_args, str(output)]
    process = None
    stderr_tail = bytearray()
    stderr_reader = None
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        stderr_reader = threading.Thread(
            target=_consume_pipe,
            args=(process.stderr, stderr_tail),
            name="formatflip-ffmpeg-stderr",
            daemon=True,
        )
        stderr_reader.start()
        try:
            return_code = process.wait(timeout=CONVERSION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            stderr_reader.join(timeout=2)
            output.unlink(missing_ok=True)
            return False, "Conversion timed out. Try a shorter or smaller file."
        stderr_reader.join(timeout=2)
    except OSError:
        app.logger.exception("Could not execute FFmpeg")
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        output.unlink(missing_ok=True)
        return False, "The converter could not start."
    if return_code != 0 or not output.is_file() or output.stat().st_size == 0:
        try:
            error_text = bytes(stderr_tail).decode("utf-8", errors="replace")
        except Exception:
            error_text = "Conversion failed"
        output.unlink(missing_ok=True)
        return False, _safe_error(error_text)
    return True, ""


def _cleanup_worker() -> None:
    while True:
        time.sleep(300)
        cutoff = time.time() - FILE_TTL_SECONDS
        for folder in (UPLOAD_DIR, OUTPUT_DIR):
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                app.logger.warning("Could not recreate temporary folder")
                continue
        with jobs_lock:
            expired_paths = [Path(value["output_path"]) for value in jobs.values()
                             if value.get("status") == "completed"
                             and value.get("created_at", 0) < cutoff
                             and value.get("output_path")]
            expired_ids = [key for key, value in jobs.items()
                           if value.get("status") != "processing"
                           and value.get("created_at", 0) < cutoff]
            output_paths_in_use = {
                value.get("output_path") for value in jobs.values()
                if value.get("status") == "completed" and value.get("output_path")
                and value.get("created_at", 0) >= cutoff
            }
            known_output_paths = {
                str(Path(value["output_path"])) for value in jobs.values()
                if value.get("output_path")
            }
            orphan_paths = [item for item in OUTPUT_DIR.iterdir()
                            if item.is_file()
                            and item.stat().st_mtime < cutoff
                            and str(item) not in known_output_paths]
            expired_paths = [path for path in expired_paths
                             if str(path) not in output_paths_in_use]
            expired_paths.extend(orphan_paths)
            for job_id in expired_ids:
                jobs.pop(job_id, None)
        for path in expired_paths:
            try:
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            except OSError:
                app.logger.warning("Could not remove expired result")


_cleanup_thread = None
_cleanup_thread_lock = threading.Lock()


def _ensure_cleanup_thread() -> None:
    """Start exactly one cleanup worker per Python process, lazily on first request."""
    global _cleanup_thread
    with _cleanup_thread_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_thread = threading.Thread(
                target=_cleanup_worker,
                name="formatflip-cleanup",
                daemon=True,
            )
            _cleanup_thread.start()


@app.before_request
def ensure_temp_dirs_and_cleanup():
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        app.logger.exception("Could not prepare temp folders")
        return jsonify({"error": "Temporary storage is unavailable."}), 503
    _ensure_cleanup_thread()
    return None


@app.errorhandler(RequestEntityTooLarge)
def too_large(_exc):
    return jsonify({"error": f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."}), 413


@app.route("/")
def index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/monetize/<path:filename>")
def monetize_files(filename: str):
    return send_from_directory(PROJECT_ROOT / "monetize", filename)


@app.route("/robots.txt")
def robots():
    return send_from_directory(PROJECT_ROOT, "robots.txt", mimetype="text/plain")


@app.route("/privacy")
def privacy():
    return send_from_directory(PROJECT_ROOT, "privacy.html")


@app.route("/terms")
def terms():
    return send_from_directory(PROJECT_ROOT, "terms.html")


@app.route("/README.md")
def readme():
    return send_from_directory(PROJECT_ROOT, "README.md")


@app.route("/health")
def health():
    ready = _find_ffmpeg() is not None
    return jsonify({"status": "ok" if ready else "degraded", "ffmpeg": ready}), (200 if ready else 503)


@app.route("/api/formats")
def formats():
    return jsonify({
        "video_to_audio": ["mp3", "wav", "aac", "m4a", "ogg", "opus", "flac"],
        "video_to_video": ["mp4", "webm", "mkv", "mov", "avi"],
        "audio_to_audio": ["mp3", "wav", "aac", "m4a", "ogg", "opus", "flac"],
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    })


@app.route("/api/convert", methods=["POST"])
def convert():
    global active_jobs
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Choose a file first."}), 400
    output_format = request.form.get("output_format", "").lower().lstrip(".")
    if output_format not in FORMATS:
        return jsonify({"error": "Unsupported output format."}), 400
    safe_name = secure_filename(upload.filename)[:96]
    input_format = Path(safe_name).suffix.lower().lstrip(".")
    if input_format not in ALLOWED_INPUTS:
        return jsonify({"error": "Unsupported input file type."}), 415
    if not _find_ffmpeg():
        return jsonify({"error": "Server converter is not configured."}), 503
    with active_jobs_lock:
        if active_jobs >= MAX_CONCURRENT_JOBS:
            return jsonify({"error": "The converter is busy. Try again shortly."}), 429
        active_jobs += 1

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}_{safe_name or 'upload'}"
    output_path = OUTPUT_DIR / f"{job_id}.{output_format}"
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "status": "processing", "progress": 5, "created_at": time.time()}
    try:
        upload.save(input_path)
        jobs[job_id]["progress"] = 20
        success, error = _run_conversion(input_path, output_path, output_format)
        if not success:
            output_path.unlink(missing_ok=True)
            with jobs_lock:
                jobs[job_id].update({"status": "failed", "progress": 100, "error": error})
            return jsonify({"error": error, "job_id": job_id}), 422
        with jobs_lock:
            jobs[job_id].update({"status": "completed", "progress": 100, "output_path": str(output_path), "filename": f"converted.{output_format}"})
        return jsonify({"job_id": job_id, "status": "completed", "download_url": f"/download/{job_id}", "filename": f"converted.{output_format}"})
    finally:
        input_path.unlink(missing_ok=True)
        with active_jobs_lock:
            active_jobs = max(0, active_jobs - 1)


@app.route("/api/status/<job_id>")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job expired or not found."}), 404
        return jsonify({key: value for key, value in job.items() if key not in {"output_path", "created_at"}})


@app.route("/download/<job_id>")
def download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        output = Path(job["output_path"]) if job and job.get("status") == "completed" else None
        filename = job.get("filename", "converted") if job else "converted"
    if not output or not output.is_file():
        return jsonify({"error": "File expired or unavailable."}), 404
    response = send_file(output, as_attachment=True, download_name=filename, conditional=True)
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    public_bind = os.getenv("PUBLIC_BIND") == "1"
    port = int(os.getenv("PORT", "5000"))
    from waitress import serve
    serve(app, host="0.0.0.0" if public_bind else "127.0.0.1", port=port, threads=4)
