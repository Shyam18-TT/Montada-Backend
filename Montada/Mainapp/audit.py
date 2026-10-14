"""
Audit trail for important actions across the app.

    from Mainapp.audit import audit
    audit("signal.created", request=request, signal=signal.pk, instrument=...)

Each call writes one ``key=value`` line to ``logs/audit.log``. Use ``audit`` for business
events (who did what to which object). Request-level traffic is logged separately by
``Mainapp.middleware.RequestAuditMiddleware`` and errors by ``log_manual_exception``.

Rules: never pass passwords, OTPs, tokens or card data. Values for keys that look secret
are redacted as a safety net, but do not rely on that. ``audit`` never raises, so logging
can not break the request it describes.
"""

import logging

logger = logging.getLogger("audit")

_SECRET_MARKERS = ("password", "otp", "token", "secret", "authorization", "card", "cvv")
_MAX_VALUE_LEN = 200


def client_ip(request):
    """Best-effort client IP (honours X-Forwarded-For set by the reverse proxy)."""
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _clean(value):
    text = str(value).replace("\n", " ").replace("\r", " ")
    if len(text) > _MAX_VALUE_LEN:
        text = text[:_MAX_VALUE_LEN] + "..."
    return f'"{text}"' if " " in text or not text else text


def audit(action, request=None, actor=None, level=logging.INFO, **fields):
    """
    Record one audit event.

    ``actor`` defaults to ``request.user`` when a request is given. ``fields`` are extra
    key=value pairs; ``None`` values are skipped.
    """
    try:
        if actor is None and request is not None:
            actor = getattr(request, "user", None)
        parts = [f"action={action}"]
        if actor is not None and getattr(actor, "is_authenticated", False):
            parts.append(f"actor={actor.pk}")
            parts.append(f"actor_type={getattr(actor, 'user_type', None) or 'unknown'}")
        elif actor is not None and not hasattr(actor, "is_authenticated"):
            parts.append(f"actor={actor}")
        ip = client_ip(request)
        if ip:
            parts.append(f"ip={ip}")
        for key, value in fields.items():
            if value is None:
                continue
            if any(marker in key.lower() for marker in _SECRET_MARKERS):
                value = "[redacted]"
            parts.append(f"{key}={_clean(value)}")
        logger.log(level, " ".join(parts))
    except Exception:  # logging must never break the caller
        logging.getLogger("app.exceptions").exception("audit logging failed action=%s", action)
