# Robbie music server
#   /                 -> "OK" (check that the server is awake)
#   /search?q=...     -> JSON: what would be played (check in a browser)
#   /prepare?q=...    -> JSON {"id","title","source"}: ESP32 calls this first
#   /stream/<id>.mp3  -> MP3 stream for ESP32 (starts immediately)
#   /play?q=...       -> search + MP3 in one step (handy for a browser test)
import os
import sys
import json
import uuid
import tempfile
import subprocess
from collections import OrderedDict

from flask import Flask, request, jsonify, Response
import yt_dlp
import imageio_ffmpeg

# deno (needed by yt-dlp for YouTube) is installed into the same venv
BIN_DIR = os.path.dirname(sys.executable)
os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ.get("PATH", "")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
YTDLP = os.path.join(BIN_DIR, "yt-dlp")

# Optional: YouTube cookies as a Render "Secret File" named cookies.txt
COOKIES = "/etc/secrets/cookies.txt"

# YouTube first, SoundCloud as backup (YouTube often blocks cloud servers)
SOURCES = ["ytsearch1:", "scsearch5:"]

app = Flask("robbie")
prepared = OrderedDict()  # id -> track info (last 20 kept)


def find_track(query):
    errors = []
    for prefix in SOURCES:
        opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": True,  # skip DRM-protected results
        }
        if prefix.startswith("yt") and os.path.exists(COOKIES):
            opts["cookiefile"] = COOKIES
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(prefix + query, download=False)
                if info and "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    info = entries[0] if entries else None
                if info and info.get("url"):
                    return ydl.sanitize_info(info)
            errors.append(prefix + " nothing found")
        except Exception as e:
            errors.append(prefix + " " + str(e)[:300])
    raise RuntimeError(" | ".join(errors))


def mp3_stream(info):
    # yt-dlp downloads the track to stdout, ffmpeg turns it into MP3 mono
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(info, tmp)
    tmp.close()
    cmd = [YTDLP, "--quiet", "--no-warnings", "--load-info-json", tmp.name, "-o", "-"]
    if os.path.exists(COOKIES):
        cmd += ["--cookies", COOKIES]
    dl = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(
        [FFMPEG, "-loglevel", "error", "-i", "pipe:0", "-vn", "-ac", "1",
         "-ar", "44100", "-b:a", "96k", "-f", "mp3", "pipe:1"],
        stdin=dl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    dl.stdout.close()

    def generate():
        try:
            while True:
                chunk = enc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            enc.kill()
            dl.kill()
            os.unlink(tmp.name)

    return Response(generate(), mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-cache"})


@app.route("/")
def home():
    return "OK"


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "no query"}), 400
    try:
        t = find_track(q)
        return jsonify({"title": t.get("title"), "source": t.get("extractor_key")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prepare")
def prepare():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "no query"}), 400
    try:
        t = find_track(q)
    except Exception as e:
        return jsonify({"error": str(e)}), 404
    track_id = uuid.uuid4().hex[:10]
    prepared[track_id] = t
    while len(prepared) > 20:
        prepared.popitem(last=False)
    return jsonify({"id": track_id, "title": t.get("title"),
                    "source": t.get("extractor_key")})


@app.route("/stream/<track_id>.mp3")
def stream(track_id):
    t = prepared.get(track_id)
    if not t:
        return "unknown id", 404
    return mp3_stream(t)


@app.route("/play")
def play():
    q = request.args.get("q", "").strip()
    if not q:
        return "no query", 400
    try:
        t = find_track(q)
    except Exception as e:
        return "not found: " + str(e), 404
    return mp3_stream(t)
