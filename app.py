# -*- coding: utf-8 -*-
"""
app.py
------
نقطة الدخول لتطبيق Flask. كل منطق yt-dlp موجود في downloader.py.
"""

import os

from flask import Flask, jsonify, render_template, request, send_from_directory

import downloader

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
app.config["JSON_AS_ASCII"] = False


def _json_payload():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def api_health():
    env = downloader.get_environment_status()
    return jsonify({
        "success": True,
        "data": env,
    })


@app.route("/api/info", methods=["POST"])
def api_info():
    payload = _json_payload()
    url = str(payload.get("url") or "").strip()

    if not url:
        return jsonify({"success": False, "error": "الرجاء إرسال رابط الفيديو (url)."}), 400

    result = downloader.extract_video_info(url)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/api/download", methods=["POST"])
def api_download():
    payload = _json_payload()
    url = str(payload.get("url") or "").strip()
    format_id = payload.get("format_id")
    audio_only = bool(payload.get("audio_only", False))
    format_has_audio = bool(payload.get("format_has_audio", False))

    result = downloader.start_download_task(
        url,
        format_id,
        audio_only=audio_only,
        format_has_audio=format_has_audio,
    )
    if result.get("success"):
        return jsonify(result), 200

    message = result.get("error", "تعذّر بدء التنزيل.")
    status = 429 if "في الانتظار" in message or "مورد تنزيل" in message else 400
    return jsonify(result), status


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"success": False, "error": "الطلب أكبر من الحجم المسموح."}), 413


@app.route("/api/progress/<task_id>", methods=["GET"])
def api_progress(task_id):
    result = downloader.get_task_status(task_id)
    return jsonify(result), 200 if result.get("success") else 404


@app.route("/downloads/<path:filename>", methods=["GET"])
def serve_downloaded_file(filename):
    # send_from_directory يستخدم secure path handling، ولا يسمح بالخروج من مجلد التنزيل.
    return send_from_directory(
        downloader.DOWNLOADS_DIR,
        filename,
        as_attachment=True,
        max_age=0,
    )


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("FLASK_PORT", "5000"))
    except ValueError:
        port = 5000
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
