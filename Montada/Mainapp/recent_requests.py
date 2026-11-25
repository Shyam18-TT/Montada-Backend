"""
Rolling log of the most recent HTTP requests and their responses.

``RecentRequestsMiddleware`` writes one JSON object per line to
``logs/recent_requests.log``. The file only ever holds the newest
``RECENT_REQUESTS_LOG_LIMIT`` entries (default 100): each new request is appended at the
bottom and the oldest lines are dropped from the top.

Several PM2 workers share the file, so every update happens under an exclusive lock on a
sidecar ``.lock`` file and the trimmed file is swapped in atomically with ``os.replace``.

Safety:
  * Values of keys that look secret (password, otp, token, ...) are replaced with
    ``***`` in JSON / form bodies and headers.
  * Bodies are truncated to ``RECENT_REQUESTS_BODY_LIMIT`` characters (default 4000).
  * Multipart uploads, binary and streaming responses are summarised, not dumped.
  * Logging never raises: a failure here can not break the request.

Settings (all optional):
  RECENT_REQUESTS_LOG_ENABLED  (bool, default True)
  RECENT_REQUESTS_LOG_LIMIT    (int,  default 100)
  RECENT_REQUESTS_BODY_LIMIT   (int,  default 4000)
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from django.conf import settings

from .audit import client_ip

_SKIP_PREFIXES = ("/static/", "/media/", "/favicon.ico")
_SECRET_MARKERS = (
    "password", "passwd", "otp", "token", "secret", "authorization", "cookie",
    "card", "cvv", "cvc", "pin", "api_key", "apikey",
)
_TEXT_TYPES = ("json", "text", "xml", "x-www-form-urlencoded", "javascript")
_LOG_HEADERS = ("CONTENT_TYPE", "CONTENT_LENGTH", "HTTP_USER_AGENT", "HTTP_AUTHORIZATION",
                "HTTP_ACCEPT_LANGUAGE", "HTTP_X_FORWARDED_FOR")


def _setting(name, default):
    return getattr(settings, name, default)


def _log_path():
    return os.path.join(str(settings.LOG_DIR), "recent_requests.log")


def _is_secret(key):
    key = str(key).lower()
    return any(marker in key for marker in _SECRET_MARKERS)


def _redact(value):
    if isinstance(value, dict):
        return {k: ("***" if _is_secret(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _truncate(text):
    limit = _setting("RECENT_REQUESTS_BODY_LIMIT", 4000)
    if len(text) > limit:
        return text[:limit] + f"... [truncated, {len(text)} chars total]"
    return text


def _decode_body(raw, content_type):
    """Turn a raw body into something readable and redacted."""
    if not raw:
        return None
    content_type = (content_type or "").lower()
    if not any(t in content_type for t in _TEXT_TYPES) and content_type:
        return f"<{content_type}, {len(raw)} bytes>"
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type or not content_type:
        try:
            parsed = _redact(json.loads(text))
            text = json.dumps(parsed, ensure_ascii=False, default=str)
        except ValueError:
            pass
    elif "x-www-form-urlencoded" in content_type:
        pairs = parse_qsl(text, keep_blank_values=True)
        text = json.dumps({k: ("***" if _is_secret(k) else v) for k, v in pairs}, ensure_ascii=False)
    return _truncate(text)


def _request_body(request):
    content_type = request.META.get("CONTENT_TYPE", "")
    if content_type.lower().startswith("multipart/"):
        # Reading request.body would load the whole upload into memory; field names
        # are logged after the view has parsed the form instead.
        return "<multipart>"
    try:
        return _decode_body(request.body, content_type)
    except Exception:
        return "<unreadable>"


def _multipart_summary(request):
    try:
        fields = {k: ("***" if _is_secret(k) else _truncate(v)) for k, v in request.POST.items()}
        files = {k: f"{f.name} ({f.size} bytes)" for k, f in request.FILES.items()}
        return {"fields": fields, "files": files}
    except Exception:
        return "<multipart>"


def _response_body(response):
    if getattr(response, "streaming", False):
        return "<streaming response>"
    try:
        return _decode_body(response.content, response.get("Content-Type", ""))
    except Exception:
        return "<unreadable>"


def _request_headers(request):
    headers = {}
    for key in _LOG_HEADERS:
        if key in request.META:
            name = key[5:] if key.startswith("HTTP_") else key
            headers[name] = "***" if _is_secret(name) else request.META[key]
    return headers


@contextmanager
def _file_lock(path):
    """Exclusive cross-process lock (works on Linux and Windows)."""
    with open(path, "a+") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_entry(entry):
    """Append ``entry`` and keep only the newest N lines in the log file."""
    path = _log_path()
    limit = max(1, int(_setting("RECENT_REQUESTS_LOG_LIMIT", 100)))
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    with _file_lock(path + ".lock"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            lines = []
        lines.append(line)
        lines = lines[-limit:]

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".recent_requests.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


class RecentRequestsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _setting("RECENT_REQUESTS_LOG_ENABLED", True) or request.path.startswith(_SKIP_PREFIXES):
            return self.get_response(request)

        # Must be read before the view: once DRF/Django consumes the stream, request.body
        # is no longer accessible.
        try:
            request_body = _request_body(request)
        except Exception:
            request_body = "<unreadable>"

        started = time.monotonic()
        response = self.get_response(request)
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            if request_body == "<multipart>":
                request_body = _multipart_summary(request)
            user = getattr(request, "user", None)
            entry = {
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "method": request.method,
                "path": request.path,
                "query": {k: ("***" if _is_secret(k) else v) for k, v in request.GET.items()},
                "user": user.pk if user is not None and getattr(user, "is_authenticated", False) else None,
                "ip": client_ip(request),
                "pid": os.getpid(),
                "request_headers": _request_headers(request),
                "request_body": request_body,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "response_type": response.get("Content-Type", ""),
                "response_body": _response_body(response),
            }
            append_entry(entry)
        except Exception:
            logging.getLogger("app.exceptions").exception("recent request logging failed")
        return response
