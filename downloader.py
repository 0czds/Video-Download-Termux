# -*- coding: utf-8 -*-
"""
downloader.py
--------------
منطق yt-dlp مع تتبع مهام التنزيل بطريقة خفيفة ومناسبة للأجهزة المحمولة.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

import yt_dlp

# تسجيل داخلي للتشخيص فقط — لا يظهر أي من هذا للمستخدم في الواجهة.
# نتائج _public_error() هي وحدها ما يصل إلى الـ API/الواجهة.
logger = logging.getLogger("video_downloader")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
ANDROID_DOWNLOADS_DIR = os.path.join("/storage/emulated/0/Download", "VideoDownloader")
DOWNLOADS_DIR = os.environ.get(
    "DOWNLOADS_DIR",
    ANDROID_DOWNLOADS_DIR if os.path.isdir("/storage/emulated/0") else _LOCAL_DOWNLOADS_DIR,
)

try:
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
except OSError:
    DOWNLOADS_DIR = _LOCAL_DOWNLOADS_DIR
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# الإعداد الافتراضي متعمد: مهمة تنزيل واحدة فقط في نفس الوقت للحفاظ على الذاكرة والبطارية.

def _safe_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


MAX_ACTIVE_DOWNLOADS = min(max(_safe_int_env("MAX_ACTIVE_DOWNLOADS", 1), 1), 2)
MAX_QUEUED_DOWNLOADS = min(max(_safe_int_env("MAX_QUEUED_DOWNLOADS", 1), 0), 2)
TASK_TTL_SECONDS = max(_safe_int_env("TASK_TTL_SECONDS", 21600), 900)
MAX_TASKS = min(max(_safe_int_env("MAX_TASKS", 50), 10), 200)
INFO_TIMEOUT_SECONDS = max(_safe_int_env("INFO_TIMEOUT_SECONDS", 35), 10)

_RUNTIME_CACHE = None


def _runtime_usable(runtime: str, path: str) -> bool:
    try:
        if runtime == "deno":
            # Deno 2.3+ is the current minimum supported by yt-dlp EJS.
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=3, check=False)
            output = result.stdout.strip() or result.stderr.strip()
            parts = output.split()[-1].split(".")
            return tuple(int(x) for x in parts[:2]) >= (2, 3)
        if runtime == "node":
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=3, check=False)
            output = (result.stdout.strip() or result.stderr.strip()).lstrip("v")
            parts = output.split(".")
            return tuple(int(x) for x in parts[:2]) >= (22, 0)
        return True
    except Exception:
        return False


def _hosts_from_env(env_name: str, default_csv: str) -> frozenset[str]:
    return frozenset(
        host.strip().lower()
        for host in os.environ.get(env_name, default_csv).split(",")
        if host.strip()
    )


# نطاقات كل منصة معرّفة بشكل صريح (whitelist)، وليست تخمينًا اعتمادًا على
# ما يتعرف عليه yt-dlp تلقائيًا. اسم متغير البيئة "YOUTUBE_ALLOWED_HOSTS" أُبقي
# كما هو للحفاظ على التوافق مع أي إعداد سابق.
YOUTUBE_ALLOWED_HOSTS = _hosts_from_env(
    "YOUTUBE_ALLOWED_HOSTS",
    "youtube.com,www.youtube.com,m.youtube.com,music.youtube.com,youtu.be,"
    "www.youtube-nocookie.com,youtube-nocookie.com",
)

TIKTOK_ALLOWED_HOSTS = _hosts_from_env(
    "TIKTOK_ALLOWED_HOSTS",
    "tiktok.com,www.tiktok.com,m.tiktok.com,vm.tiktok.com,vt.tiktok.com",
)

INSTAGRAM_ALLOWED_HOSTS = _hosts_from_env(
    "INSTAGRAM_ALLOWED_HOSTS",
    "instagram.com,www.instagram.com,instagr.am,www.instagr.am",
)

def _build_platform_host_map() -> dict[str, str]:
    """خريطة نطاق → اسم منصة، تُستخدم من detect_platform() ومن is_valid_url()
    كمصدر حقيقة واحد بدل تكرار فحوصات النطاقات في أماكن متعددة."""
    mapping: dict[str, str] = {}
    for host in YOUTUBE_ALLOWED_HOSTS:
        mapping[host] = "youtube"
    for host in TIKTOK_ALLOWED_HOSTS:
        mapping[host] = "tiktok"
    for host in INSTAGRAM_ALLOWED_HOSTS:
        mapping[host] = "instagram"
    return mapping


_PLATFORM_HOSTS: dict[str, str] = _build_platform_host_map()
ALLOWED_HOSTS = frozenset(_PLATFORM_HOSTS)

DOWNLOAD_TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.RLock()
DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_DOWNLOADS)
INFO_SLOT = threading.BoundedSemaphore(1)
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def _find_js_runtime() -> tuple[str | None, str | None]:
    """اختر runtime متاحًا ومتوافقًا بدون تثبيت أي شيء تلقائيًا."""
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None:
        return _RUNTIME_CACHE

    configured_value = os.environ.get("YTDLP_JS_RUNTIME", "auto").strip()
    configured = configured_value.lower()
    candidates = []
    configured_path = None
    if configured and configured != "auto":
        runtime_raw, sep, custom_path = configured_value.partition(":")
        candidates = [runtime_raw.strip().lower()]
        configured_path = custom_path.strip() if sep and custom_path.strip() else None
    else:
        candidates = ["deno", "node", "qjs"]

    for runtime in candidates:
        if not runtime:
            continue
        path = configured_path if configured_path else shutil.which(runtime)
        if path and os.path.isfile(path) and os.access(path, os.X_OK) and (runtime == "qjs" or _runtime_usable(runtime, path)):
            _RUNTIME_CACHE = (runtime, path)
            return _RUNTIME_CACHE

    _RUNTIME_CACHE = (None, None)
    return _RUNTIME_CACHE


def _base_ydl_opts(extra_headers=None, for_download=False):
    """خيارات yt-dlp الأساسية.

    ``for_download=True`` تُستخدم فقط أثناء التنزيل الفعلي للملف (وليس
    لجلب المعلومات)، لأن مهلة الاتصال (socket_timeout) المناسبة لسؤال
    سريع عن بيانات الفيديو مختلفة تمامًا عن المهلة المناسبة لتيار تنزيل
    طويل قد يتباطأ مؤقتًا على شبكة هاتف دون أن يكون فعليًا متوقفًا.
    مهلة قصيرة جدًا أثناء تنزيل فيديو كبير/بدقة عالية تعني قطع الاتصال
    وإعادة المحاولة من جديد لمجرد تباطؤ عابر في الشبكة، وهذا يُبطئ
    التنزيل فعليًا بدل أن يحميه.
    """
    configured_clients = os.environ.get("YTDLP_PLAYER_CLIENTS", "").strip()

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 60 if for_download else 20,
        "retries": 10 if for_download else 3,
        "fragment_retries": 10 if for_download else 3,
        "file_access_retries": 2,
        "continuedl": True,
        # تنزيل عدة أجزاء (fragments) بالتوازي يسرّع التحميل بشكل ملحوظ على
        # الروابط المجزأة (DASH/HLS) دون إرهاق ذاكرة الهاتف. 4 خيوط متوازية
        # توازن جيدًا بين السرعة واستهلاك الموارد على جهاز محمول.
        "concurrent_fragment_downloads": 4,
        "ignoreerrors": False,
        # تهدئة تصاعدية بين محاولات إعادة الاتصال بدل الضرب الفوري على شبكة
        # قد تكون بطيئة مؤقتًا فقط (شائع على بيانات الهاتف المحمول).
        "retry_sleep_functions": {
            "http": lambda n: min(1 + n, 10),
            "fragment": lambda n: min(1 + n, 10),
        },
    }

    if extra_headers:
        opts["http_headers"] = dict(extra_headers)

    if configured_clients:
        player_clients = [item.strip() for item in configured_clients.split(",") if item.strip()]
        if player_clients:
            opts["extractor_args"] = {"youtube": {"player_client": player_clients}}

    runtime, path = _find_js_runtime()
    if runtime and path:
        opts["js_runtimes"] = {runtime: {}}

    return opts


def get_environment_status() -> dict:
    runtime, runtime_path = _find_js_runtime()
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    try:
        version = yt_dlp.version.__version__
    except Exception:
        version = "غير معروف"

    return {
        "yt_dlp": version,
        "ffmpeg": bool(ffmpeg),
        "ffprobe": bool(ffprobe),
        "js_runtime": runtime,
        "js_runtime_path": runtime_path,
        "full_youtube_support": bool(runtime and ffmpeg and ffprobe),
        # يعني "true" أن extractor الخاص بالمنصة مفعّل ومدعوم داخل yt-dlp في
        # هذا الإصدار — وليس ضمانًا أن كل رابط أو فيديو سيعمل فعليًا.
        "platforms": {
            "youtube": True,
            "tiktok": True,
            "instagram": True,
        },
    }


def _human_readable_size(num_bytes):
    if not num_bytes:
        return "غير معروف"
    num_bytes = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _format_height(f) -> int:
    try:
        return int(f.get("height") or 0)
    except (TypeError, ValueError):
        return 0


def _format_fps(f) -> float:
    try:
        return float(f.get("fps") or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_bitrate(f) -> float:
    for key in ("vbr", "tbr"):
        try:
            value = float(f.get(key) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 0.0


STANDARD_VIDEO_HEIGHTS = (1080, 720, 480, 360, 240, 144)


def _classify_formats(formats):
    """Return the six UI quality tiers requested by the app, plus a safe
    fallback list for platforms whose formats never hit those exact tiers.

    We group by the actual video height, not by ``format_note`` or width.
    This prevents values such as 1920x1080, 840p, or other provider-specific
    labels from leaking into the UI as if they were one of the six standard
    tiers. For each tier we keep the best actual format, preferring higher
    FPS and then higher bitrate.

    Some platforms (TikTok/Instagram in particular) frequently only expose
    non-standard heights (e.g. 640, 852, 1024). Silently dropping every
    format would leave a video with metadata but nothing to download. So
    when a video has video-capable formats but none land on a standard
    tier, we surface those raw formats separately as ``other_qualities`` —
    each honestly labeled with its real height, never renamed to a
    standard tier it doesn't match.
    """
    audio_only = []
    other_video = []
    best_by_quality: dict[int, tuple[dict, dict, tuple]] = {}

    for f in formats:
        ext = (f.get("ext") or "").lower()
        if ext == "mhtml":
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        if not has_video and not has_audio:
            continue

        height = _format_height(f)
        width = f.get("width")
        fps = _format_fps(f)

        if has_video:
            entry = {
                "format_id": f.get("format_id"),
                "ext": ext,
                "resolution": f"{height}p" if height else "جودة غير معروفة",
                "quality": height if height in STANDARD_VIDEO_HEIGHTS else None,
                "width": width,
                "height": height or None,
                "fps": fps or None,
                "filesize": _human_readable_size(f.get("filesize") or f.get("filesize_approx")),
                "abr": f.get("abr"),
                "vbr": f.get("vbr"),
                "tbr": f.get("tbr"),
                "has_audio": has_audio,
            }

            # One entry per visible quality. Prefer FPS first so a real 60fps
            # 1080p stream wins over a 30fps stream at the same height.
            score = (
                _format_fps(f),
                _format_bitrate(f),
                1 if ext == "mp4" else 0,
                1 if str(vcodec or "").lower().startswith("avc1") else 0,
                1 if has_audio else 0,
            )

            if height in STANDARD_VIDEO_HEIGHTS:
                current = best_by_quality.get(height)
                if current is None or score > current[2]:
                    best_by_quality[height] = (entry, f, score)
            elif height:
                # Non-standard height: kept aside, never relabeled as a
                # standard tier. Deduplicated by height, keeping the best.
                other_video.append((entry, score))
        else:
            # Keep only valid audio streams for the MP3/audio workflow.
            audio_only.append({
                "format_id": f.get("format_id"),
                "ext": ext,
                "resolution": "صوت فقط",
                "quality": None,
                "width": width,
                "height": None,
                "fps": None,
                "filesize": _human_readable_size(f.get("filesize") or f.get("filesize_approx")),
                "abr": f.get("abr"),
                "vbr": f.get("vbr"),
                "tbr": f.get("tbr"),
                "has_audio": True,
            })

    qualities = []
    for height in STANDARD_VIDEO_HEIGHTS:
        current = best_by_quality.get(height)
        if current:
            qualities.append(current[0])

    # Fallback list only matters when no standard tier was found at all —
    # if the video already has real standard-tier formats, we don't clutter
    # the UI with non-standard extras.
    other_qualities = []
    if not qualities and other_video:
        best_other: dict[int, tuple[dict, tuple]] = {}
        for entry, score in other_video:
            h = entry["height"]
            current = best_other.get(h)
            if current is None or score > current[1]:
                best_other[h] = (entry, score)
        other_qualities = [
            item[0] for item in sorted(best_other.values(), key=lambda item: item[0]["height"], reverse=True)
        ][:6]

    audio_only.sort(key=lambda f: float(f.get("abr") or 0), reverse=True)

    return {
        "video_only": [f for f in qualities if not f.get("has_audio")],
        "audio_only": audio_only,
        "combined": [f for f in qualities if f.get("has_audio")],
        "qualities": qualities,
        "other_qualities": other_qualities,
    }


def detect_platform(url) -> str:
    """يحدد المنصة من الرابط عبر مطابقة النطاق الحقيقي (urlsplit)، وليس
    عبر البحث عن كلمة داخل الرابط. يُستخدم من is_valid_url() وextract_video_info()
    كمصدر واحد بدل تكرار فحوصات النطاقات في أماكن متعددة."""
    if not url or not isinstance(url, str):
        return "unknown"
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return "unknown"
    return _PLATFORM_HOSTS.get(host, "unknown")


def is_valid_url(url):
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if len(url) > 4096 or not re.match(r"^https?://", url, re.IGNORECASE):
        return False

    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password or parsed.fragment:
            return False
        return host in ALLOWED_HOSTS
    except ValueError:
        return False


def _clean_error(exc: Exception, limit: int = 600) -> str:
    message = " ".join(str(exc).split())
    if len(message) > limit:
        message = message[:limit].rstrip() + "…"
    return message or "خطأ غير معروف."


_PLATFORM_DISPLAY_NAMES = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
}


def _public_error(exc: Exception, platform: str | None = None) -> str:
    """يحوّل استثناء yt-dlp/ffmpeg/شبكة إلى رسالة عربية مفهومة للمستخدم.

    ``platform`` اختياري: عندما تكون المنصة معروفة نخصص بعض الرسائل باسمها
    (وأحيانًا بنص خاص بـTikTok/Instagram)، وإلا نستخدم صياغة عامة تناسب أي
    منصة من المنصات الثلاث المدعومة.
    """
    msg = _clean_error(exc).lower()
    platform_name = _PLATFORM_DISPLAY_NAMES.get(platform or "")

    if "private" in msg or "sign in" in msg or "login" in msg or "log in" in msg:
        if platform == "instagram":
            return "هذا المحتوى من Instagram غير متاح للتنزيل حاليًا، أو يتطلب تسجيل الدخول."
        if platform == "tiktok":
            return "تعذر الوصول إلى هذا الفيديو من TikTok. قد يكون الفيديو خاصًا أو محذوفًا أو يتطلب صلاحيات إضافية."
        return "هذا المحتوى خاص ويتطلب صلاحيات إضافية، أو يتطلب تسجيل الدخول ولا يمكن للتطبيق الوصول إليه بهذه الحالة."
    if "unsupported url" in msg or "is not a valid url" in msg or "no extractor" in msg:
        return "الرابط غير مدعوم أو غير صالح."
    if ("rate" in msg and "limit" in msg) or "429" in msg or "too many requests" in msg:
        return "الخدمة رفضت الطلبات مؤقتًا. انتظر قليلًا ثم أعد المحاولة."
    # فحص "timed out" قبل الفحص العام لكلمة "connection"، لأن رسائل timeout
    # الفعلية من مكتبات الشبكة غالبًا تحتوي الكلمتين معًا (مثل
    # "Connection timed out")، ونريد رسالة timeout الأدق أن تُطابق أولًا.
    if "timed out" in msg or "timeout" in msg:
        return "انتهت مهلة الاتصال. أعد المحاولة."
    if "unable to download webpage" in msg or "urlopen error" in msg or "connection" in msg:
        if platform_name:
            return f"تعذّر الاتصال بخدمة {platform_name}. تحقق من اتصال الإنترنت ثم أعد المحاولة."
        return "تعذّر الاتصال بالخدمة. تحقق من اتصال الإنترنت ثم أعد المحاولة."
    if "video unavailable" in msg or "content is not available" in msg or "not available" in msg:
        if platform == "tiktok":
            return "تعذر الوصول إلى هذا الفيديو من TikTok. قد يكون الفيديو خاصًا أو محذوفًا أو يتطلب صلاحيات إضافية."
        if platform == "instagram":
            return "هذا المحتوى من Instagram غير متاح للتنزيل حاليًا، أو يتطلب تسجيل الدخول."
        return "تعذّر العثور على الفيديو. ربما تم حذفه أو أصبح غير متاح، أو أنه محظور في منطقتك."
    if "sign in to confirm" in msg or "confirm you're not a bot" in msg:
        return "YouTube يطلب تحققًا إضافيًا لهذا الطلب. جرّب تحديث yt-dlp وإعداد JavaScript runtime."
    if "requested format is not available" in msg or "no video formats found" in msg:
        return "الصيغة المطلوبة غير متاحة لهذا الفيديو. أعد جلب البيانات واختَر صيغة أخرى."
    if "unsupported" in msg:
        return "هذا النوع من المحتوى غير مدعوم حاليًا."
    if "ffmpeg" in msg or "ffprobe" in msg:
        return "يلزم تثبيت ffmpeg لإكمال هذه العملية."
    if "file too large" in msg:
        return f"حجم هذا الفيديو أكبر من الحد المسموح ({MAX_DOWNLOAD_MB}MB). اختر جودة أقل."
    if "low disk space" in msg:
        return "توقف التنزيل لأن مساحة التخزين المتبقية على الجهاز أصبحت منخفضة جدًا. حرّر بعض المساحة ثم أعد المحاولة."
    if "no space" in msg or "permission denied" in msg or "errno 13" in msg or "errno 28" in msg:
        return "تعذّر حفظ الملف. تحقق من مساحة التخزين وصلاحيات مجلد التنزيل."
    return "حدث خطأ غير متوقع أثناء معالجة الفيديو."


def _prune_tasks_locked(now=None):
    now = now or time.time()
    expired = [
        task_id
        for task_id, task in DOWNLOAD_TASKS.items()
        if task.get("status") in {"completed", "error"}
        and now - float(task.get("updated_at", now)) > TASK_TTL_SECONDS
    ]
    for task_id in expired:
        DOWNLOAD_TASKS.pop(task_id, None)

    if len(DOWNLOAD_TASKS) <= MAX_TASKS:
        return

    removable = sorted(
        (
            (float(task.get("updated_at", 0)), task_id)
            for task_id, task in DOWNLOAD_TASKS.items()
            if task.get("status") in {"completed", "error"}
        ),
        key=lambda item: item[0],
    )
    for _, task_id in removable:
        if len(DOWNLOAD_TASKS) <= MAX_TASKS:
            break
        DOWNLOAD_TASKS.pop(task_id, None)


def extract_video_info(url):
    if not is_valid_url(url):
        return {"success": False, "error": "الرابط غير صالح أو المنصة غير مدعومة."}

    platform = detect_platform(url)

    if not INFO_SLOT.acquire(timeout=1):
        return {"success": False, "error": "السيرفر مشغول بجلب بيانات فيديو آخر. أعد المحاولة بعد لحظات."}

    try:
        opts = _base_ydl_opts()
        opts["socket_timeout"] = INFO_TIMEOUT_SECONDS
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            return {"success": False, "error": "تعذّر العثور على الفيديو. ربما تم حذفه أو أصبح غير متاح."}

        # بيانات yt-dlp تُعتبر غير موثوقة وقد تكون ناقصة بحسب المنصة، لذلك
        # لكل حقل قيمة احتياطية آمنة بدل تمرير None إلى الواجهة.
        raw_formats = info.get("formats") or []
        if not raw_formats and (info.get("url") or info.get("ext")):
            # بعض المستخرجات (شائع في TikTok/Instagram) تعيد صيغة واحدة
            # مباشرة على مستوى "info" بدل قائمة "formats". نحوّلها لنفس الشكل
            # حتى لا يفشل _classify_formats بصمت ويترك المستخدم بلا أي خيار تنزيل.
            raw_formats = [{
                "format_id": info.get("format_id") or "0",
                "ext": info.get("ext"),
                "vcodec": info.get("vcodec") or "unknown",
                "acodec": info.get("acodec") or "unknown",
                "height": info.get("height"),
                "width": info.get("width"),
                "fps": info.get("fps"),
                "filesize": info.get("filesize"),
                "filesize_approx": info.get("filesize_approx"),
                "abr": info.get("abr"),
                "vbr": info.get("vbr"),
                "tbr": info.get("tbr"),
            }]
        formats = _classify_formats(raw_formats)
        env = get_environment_status()
        runtime_warning = None
        if platform == "youtube" and not env["js_runtime"]:
            runtime_warning = "لم يتم العثور على JavaScript runtime؛ قد تكون بعض صيغ YouTube غير متاحة."

        thumbnail = info.get("thumbnail")
        if thumbnail and not re.match(r"^https?://", str(thumbnail), re.IGNORECASE):
            thumbnail = None

        return {
            "success": True,
            "data": {
                "id": info.get("id") or "—",
                "title": info.get("title") or "بدون عنوان",
                "thumbnail": thumbnail,
                "duration": info.get("duration"),
                "duration_string": info.get("duration_string"),
                "uploader": info.get("uploader") or "حساب غير معروف",
                "webpage_url": info.get("webpage_url") or url,
                "formats": formats,
                "platform": platform,
                "environment_warning": runtime_warning,
            },
        }

    except yt_dlp.utils.DownloadError as exc:
        logger.warning("extract_video_info DownloadError (platform=%s): %s", platform, _clean_error(exc))
        return {"success": False, "error": _public_error(exc, platform)}
    except Exception as exc:
        logger.exception("extract_video_info unexpected error (platform=%s)", platform)
        return {"success": False, "error": "حدث خطأ غير متوقع أثناء معالجة الفيديو."}
    finally:
        INFO_SLOT.release()


def _task_update(task_id, **changes):
    now = time.time()
    with TASKS_LOCK:
        task = DOWNLOAD_TASKS.get(task_id)
        if task is None:
            return
        task.update(changes)
        task["updated_at"] = now
        _prune_tasks_locked(now)


def _download_guard_error(total_bytes, downloaded_bytes, free_mb, needs_merge) -> str | None:
    """يقرر إن كان يجب إيقاف التنزيل حماية للجهاز، ويعيد نص السبب (داخليًا)
    أو None إذا كان كل شيء سليمًا. دالة صرفة بلا آثار جانبية لتسهيل اختبارها.

    - حجم الملف المعروف أكبر من MAX_DOWNLOAD_MB  -> رفض.
    - المساحة الحرة أقل من (المتبقي للتنزيل + هامش MIN_FREE_DISK_MB)  -> رفض.
      وعند الحاجة لدمج ffmpeg نضيف حجم الملف مرة أخرى لأن الدمج ينتج نسخة
      جديدة بجانب الأصلية مؤقتًا.
    - عند عدم معرفة الحجم (شائع في TikTok/Instagram) نكتفي بالحد الأدنى.
    """
    if free_mb is not None and free_mb < MIN_FREE_DISK_MB:
        return "low disk space"

    if not total_bytes:
        return None

    total_mb = total_bytes / (1024 * 1024)
    if MAX_DOWNLOAD_MB and total_mb > MAX_DOWNLOAD_MB:
        return "file too large"

    if free_mb is not None:
        remaining_mb = max(total_bytes - (downloaded_bytes or 0), 0) / (1024 * 1024)
        merge_extra_mb = total_mb if needs_merge else 0
        if free_mb < remaining_mb + merge_extra_mb + MIN_FREE_DISK_MB:
            return "low disk space"
    return None


def _make_progress_hook(task_id, needs_merge=False):
    # فحص مساحة القرص مكلف نسبيًا، فنكرره كل بضع ثوانٍ فقط (وفي أول استدعاء
    # لنعرف مبكرًا إن كان الفيديو أكبر من المتاح) بدل كل استدعاء للـ hook.
    last_disk_check = {"t": 0.0}

    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0

            now = time.time()
            if now - last_disk_check["t"] > 5:
                last_disk_check["t"] = now
                reason = _download_guard_error(
                    total, downloaded, _free_disk_mb(DOWNLOADS_DIR), needs_merge
                )
                if reason:
                    # رفع استثناء داخل الـ hook يوقف yt-dlp. الملفات الجزئية
                    # (.part) تبقى فيستطيع المستخدم الاستئناف بعد تحرير المساحة.
                    raise yt_dlp.utils.DownloadError(reason)

            _task_update(
                task_id,
                status="downloading",
                percent=round(max(0, min(99, percent)), 1),
                speed=_human_readable_size(d.get("speed") or 0) + "/s" if d.get("speed") else "",
                eta=d.get("eta"),
            )
        elif status == "finished":
            _task_update(
                task_id,
                percent=99.0,
                status="finalizing",
                speed="",
                eta=None,
            )
        elif status == "error":
            _task_update(task_id, status="error", error="حدث خطأ أثناء التنزيل.")

    return hook


def _make_postprocessor_hook(task_id):
    def hook(d):
        status = d.get("status")
        name = str(d.get("postprocessor") or "").lower()
        if status == "started":
            phase = "تحويل الصوت..." if "extractaudio" in name else "جاري تجهيز الملف..."
            if "merger" in name:
                phase = "جارٍ دمج الصوت والفيديو..."
            _task_update(task_id, status="postprocessing", percent=99, phase=phase)
        elif status == "finished":
            _task_update(task_id, status="finalizing", percent=99, phase="جاري حفظ الملف...")

    return hook


def _task_capacity_available_locked():
    active = sum(
        task.get("status") in {"downloading", "finalizing", "postprocessing"}
        for task in DOWNLOAD_TASKS.values()
    )
    queued = sum(task.get("status") == "queued" for task in DOWNLOAD_TASKS.values())
    return active < MAX_ACTIVE_DOWNLOADS or queued < MAX_QUEUED_DOWNLOADS


def _validate_format_choice(format_choice):
    if format_choice in (None, ""):
        return True
    return bool(FORMAT_ID_RE.fullmatch(str(format_choice)))


# حد أدنى من المساحة الحرة يجب توفره دائمًا على الجهاز (بالميغابايت)، حتى
# عندما لا نعرف حجم الفيديو مسبقًا. يحمي من امتلاء التخزين بالكامل الذي قد
# يعطّل النظام نفسه على بعض أجهزة أندرويد، وليس فقط تطبيق التنزيل.
MIN_FREE_DISK_MB = max(_safe_int_env("MIN_FREE_DISK_MB", 250), 50)

# أقصى حجم مسموح لملف واحد بالميغابايت (0 = بلا حد). يحمي الهاتف من فيديو
# ضخم جدًا (دقة عالية + مدة طويلة) قد يستغرق ساعات أو يملأ التخزين.
MAX_DOWNLOAD_MB = max(_safe_int_env("MAX_DOWNLOAD_MB", 4096), 0)


def _free_disk_mb(path) -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 * 1024)
    except OSError:
        # لا يمكن قراءة مساحة القرص (نظام ملفات غير معتاد مثلًا) — لا نعيق
        # التنزيل بسبب فشل الفحص نفسه، فقط نتخطى الحماية بأمان.
        return None


def start_download_task(url, format_choice, audio_only=False, format_has_audio=False):
    if not is_valid_url(url):
        return {"success": False, "error": "الرابط غير صالح أو المنصة غير مدعومة."}

    platform = detect_platform(url)

    if not audio_only and not _validate_format_choice(format_choice):
        return {"success": False, "error": "معرّف الصيغة غير صالح."}

    needs_ffmpeg = bool(audio_only or not format_has_audio)
    if needs_ffmpeg and not shutil.which("ffmpeg"):
        return {"success": False, "error": "هذه العملية تحتاج ffmpeg. ثبّته عبر: pkg install ffmpeg"}
    if needs_ffmpeg and not shutil.which("ffprobe"):
        return {"success": False, "error": "هذه العملية تحتاج ffprobe المرفق مع ffmpeg. تحقق من تثبيت ffmpeg بالكامل."}

    free_mb = _free_disk_mb(DOWNLOADS_DIR)
    if free_mb is not None and free_mb < MIN_FREE_DISK_MB:
        return {
            "success": False,
            "error": f"مساحة التخزين المتبقية منخفضة جدًا ({free_mb:.0f}MB). حرّر بعض المساحة ثم أعد المحاولة.",
        }

    task_id = str(uuid.uuid4())
    now = time.time()

    with TASKS_LOCK:
        _prune_tasks_locked(now)
        if not _task_capacity_available_locked():
            return {
                "success": False,
                "error": "يوجد تنزيل قيد التنفيذ ومهمة أخرى في الانتظار. انتظر اكتمال إحداهما ثم أعد المحاولة.",
            }

        DOWNLOAD_TASKS[task_id] = {
            "status": "queued",
            "percent": 0,
            "filename": None,
            "error": None,
            "speed": "",
            "eta": None,
            "phase": "في قائمة الانتظار...",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }

    outtmpl = os.path.join(DOWNLOADS_DIR, "%(title).100s-%(id)s.%(ext)s")
    ydl_opts = _base_ydl_opts(for_download=True)
    ydl_opts.update(
        {
            "outtmpl": outtmpl,
            "progress_hooks": [_make_progress_hook(task_id, needs_merge=needs_ffmpeg)],
            "postprocessor_hooks": [_make_postprocessor_hook(task_id)],
            "overwrites": False,
        }
    )

    # نحدّ عدد خيوط ffmpeg حتى لا تستهلك عملية الدمج/الترميز كل أنوية
    # المعالج على الهاتف (مهم خصوصًا مع فيديو كبير الحجم أو بدقة عالية،
    # حيث تصبح عملية ffmpeg أطول وأثقل). نصف الأنوية المتاحة أو 2 كحد
    # أدنى يوازن بين سرعة معقولة وعدم تجميد بقية النظام أثناء المعالجة.
    ffmpeg_threads = str(max(2, (os.cpu_count() or 2) // 2))

    if audio_only:
        ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
        ydl_opts["postprocessor_args"] = {"ExtractAudio": ["-threads", ffmpeg_threads]}
    elif format_choice:
        if format_has_audio:
            ydl_opts["format"] = str(format_choice)
        else:
            # نفضّل M4A/AAC للصوت عند توفره لتقليل مشاكل التوافق والمزامنة في MP4.
            ydl_opts["format"] = (
                f"{format_choice}+bestaudio[ext=m4a]/"
                f"{format_choice}+bestaudio/"
                f"{format_choice}"
            )
            ydl_opts["merge_output_format"] = "mp4"
            # إبقاء الفيديو كما هو (stream copy، بلا إعادة ترميز) وإعادة
            # ترميز الصوت فقط مع تصحيح timestamps. "-threads" يحدّ استهلاك
            # المعالج، وهو تأثير طفيف هنا لأن الفيديو أصلًا copy بلا ترميز،
            # لكنه يبقي هامش أمان على الهواتف الأضعف.
            ydl_opts["postprocessor_args"] = {
                "Merger+ffmpeg_o": [
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-af", "aresample=async=1:first_pts=0",
                    "-threads", ffmpeg_threads,
                ]
            }
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessor_args"] = {
            "Merger+ffmpeg_o": [
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-af", "aresample=async=1:first_pts=0",
                "-threads", ffmpeg_threads,
            ]
        }

    thread = threading.Thread(
        target=_run_download,
        args=(task_id, url, ydl_opts, needs_ffmpeg, platform),
        name=f"download-{task_id[:8]}",
        daemon=True,
    )
    thread.start()
    return {"success": True, "task_id": task_id}


def _run_download(task_id, url, ydl_opts, needs_ffmpeg, platform=None):
    acquired = DOWNLOAD_SLOTS.acquire()
    if not acquired:
        _task_update(task_id, status="error", error="تعذّر توفير مورد تنزيل متاح على الجهاز.")
        return

    try:
        _task_update(task_id, status="downloading", started_at=time.time(), phase="جارٍ التنزيل...")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_filename = ydl.prepare_filename(info)

            if ydl_opts.get("postprocessors"):
                base, _ = os.path.splitext(final_filename)
                final_filename = base + ".mp3"
            elif ydl_opts.get("merge_output_format"):
                base, _ = os.path.splitext(final_filename)
                final_filename = base + "." + ydl_opts["merge_output_format"]

        final_filename = os.path.basename(final_filename)
        final_path = os.path.join(DOWNLOADS_DIR, final_filename)
        if not os.path.isfile(final_path):
            # fallback للصيغ المدمجة التي يقرر yt-dlp فيها الامتداد النهائي وفق codec/container.
            candidates = []
            prefix = os.path.splitext(final_filename)[0]
            try:
                candidates = [
                    os.path.join(DOWNLOADS_DIR, name)
                    for name in os.listdir(DOWNLOADS_DIR)
                    if name.startswith(prefix + ".") and os.path.isfile(os.path.join(DOWNLOADS_DIR, name))
                ]
            except OSError:
                candidates = []
            if len(candidates) == 1:
                final_filename = os.path.basename(candidates[0])
                final_path = candidates[0]

        if not os.path.isfile(final_path):
            raise FileNotFoundError("تم التنزيل لكن لم يتم العثور على الملف النهائي.")

        _task_update(
            task_id,
            status="completed",
            percent=100,
            filename=final_filename,
            error=None,
            speed="",
            eta=None,
            phase="اكتمل التنزيل.",
            finished_at=time.time(),
        )

    except yt_dlp.utils.DownloadError as exc:
        logger.warning("_run_download DownloadError (platform=%s, task=%s): %s", platform, task_id, _clean_error(exc))
        _task_update(
            task_id,
            status="error",
            percent=0,
            error=_public_error(exc, platform),
            phase="فشل التنزيل.",
            finished_at=time.time(),
        )
    except Exception:
        logger.exception("_run_download unexpected error (platform=%s, task=%s)", platform, task_id)
        _task_update(
            task_id,
            status="error",
            percent=0,
            error="حدث خطأ غير متوقع أثناء معالجة الفيديو.",
            phase="فشل التنزيل.",
            finished_at=time.time(),
        )
    finally:
        # يضمن تحرير المورد دائمًا — حتى لو انهار أي جزء أعلاه بشكل غير متوقع —
        # بحيث لا يبقى النظام عالقًا وغير قادر على قبول مهمة تنزيل جديدة.
        DOWNLOAD_SLOTS.release()


def get_task_status(task_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{20,64}", str(task_id or "")):
        return {"success": False, "error": "معرّف المهمة غير صالح."}

    with TASKS_LOCK:
        _prune_tasks_locked()
        task = DOWNLOAD_TASKS.get(task_id)
        if task is None:
            return {"success": False, "error": "معرّف المهمة غير موجود أو انتهت صلاحيته."}
        return {"success": True, "data": dict(task)}
