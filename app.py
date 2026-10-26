"""Web UI for controlling a Canon EOS M50 over USB.

Run:  ./run.sh   then open http://localhost:5050
"""

import logging
import os
import signal
import subprocess

from flask import Flask, Response, jsonify, request, send_from_directory

from camera import CAPTURE_DIR, CameraController, CameraError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

app = Flask(__name__, static_folder="static")
cam = CameraController()


def run(fn, *args):
    try:
        return jsonify(ok=True, result=cam.call(fn, *args))
    except CameraError as e:
        return jsonify(ok=False, error=str(e)), 400
    except TimeoutError:
        return jsonify(ok=False, error="המצלמה לא הגיבה בזמן"), 504


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    return jsonify(cam.status())


@app.get("/api/debug/events")
def debug_events():
    return jsonify(cam.raw_events)


@app.post("/api/record/start")
def record_start():
    return run(cam.cmd_start_recording)


@app.post("/api/record/stop")
def record_stop():
    return run(cam.cmd_stop_recording)


@app.post("/api/photo")
def photo():
    return run(cam.cmd_take_photo)


@app.post("/api/autofocus")
def autofocus():
    return run(cam.cmd_autofocus)


@app.post("/api/sync-clock")
def sync_clock():
    return run(cam.cmd_sync_clock)


@app.post("/api/reconnect")
def reconnect():
    return run(cam.cmd_reconnect)


@app.get("/api/settings")
def settings():
    return run(cam.cmd_get_settings)


@app.post("/api/settings")
def set_setting():
    body = request.get_json(force=True)
    return run(cam.cmd_set_setting, body["key"], body["value"])


@app.post("/api/options")
def options():
    """App-side options that need no camera access."""
    body = request.get_json(force=True)
    if "auto_restart" in body:
        cam.auto_restart = bool(body["auto_restart"])
    if "split_minutes" in body:
        cam.split_minutes = max(0, int(body["split_minutes"] or 0))
    if "download_photos" in body:
        cam.download_photos = bool(body["download_photos"])
    if "preview" in body:
        return run(cam.cmd_set_preview, bool(body["preview"]))
    return jsonify(ok=True)


@app.post("/api/movie-quality")
def movie_quality():
    body = request.get_json(force=True)
    return run(cam.cmd_set_movie_quality, int(body["size"]), int(body["fps100"]))


@app.post("/api/schedule")
def schedule():
    """Body: {start, stop} as epoch seconds or null. Empty body cancels."""
    body = request.get_json(force=True) or {}
    try:
        cam.set_schedule(body.get("start"), body.get("stop"))
    except CameraError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/download")
def download():
    body = request.get_json(force=True)
    return run(cam.cmd_download, body["folder"], body["name"])


@app.get("/captures/<path:name>")
def captures(name):
    return send_from_directory(CAPTURE_DIR, name)


@app.get("/preview.mjpg")
def preview():
    def frames():
        seq = -1
        while cam.preview_on:
            with cam.frame_ready:
                cam.frame_ready.wait_for(lambda: cam.preview_seq != seq or not cam.preview_on, timeout=1)
                seq, frame = cam.preview_seq, cam.preview_frame
            if frame:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


RESTART_CODE = 75  # serve.sh restarts on any non-zero exit


def shutdown(signum, frame):
    try:
        cam.call(cam.cmd_shutdown, timeout=8)
    except Exception:
        pass
    # SIGHUP = clean restart (e.g. to load new code); TERM/INT = quit
    os._exit(RESTART_CODE if signum == signal.SIGHUP else 0)


if __name__ == "__main__":
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, shutdown)
    # keep the Mac from idle-sleeping (and missing a schedule) while we run
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    cam.start()
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, threaded=True)
