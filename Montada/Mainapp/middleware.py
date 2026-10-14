"""
Request-level audit logging.

Logs every state-changing API call (POST/PUT/PATCH/DELETE) and every failed request
(4xx/5xx) to the ``audit`` logger with user, status and duration. Plain successful GETs
are skipped to keep the log readable; use the DB timing log for read performance.

Request bodies are never logged (they carry passwords, OTPs and tokens).
"""

import logging
import time

from .audit import client_ip

logger = logging.getLogger("audit.requests")

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_SKIP_PREFIXES = ("/static/", "/media/")


class RequestAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        try:
            self._log(request, response, time.monotonic() - started)
        except Exception:
            logging.getLogger("app.exceptions").exception("request audit logging failed")
        return response

    @staticmethod
    def _log(request, response, duration):
        path = request.path
        if path.startswith(_SKIP_PREFIXES):
            return
        status = response.status_code
        if request.method not in _MUTATING and status < 400:
            return
        # DRF copies the authenticated user onto the Django request, so JWT users show here.
        user = getattr(request, "user", None)
        user_id = user.pk if user is not None and getattr(user, "is_authenticated", False) else "anonymous"
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        logger.log(
            level,
            "request method=%s path=%s status=%s user=%s ip=%s duration_ms=%d",
            request.method,
            path,
            status,
            user_id,
            client_ip(request),
            int(duration * 1000),
        )
