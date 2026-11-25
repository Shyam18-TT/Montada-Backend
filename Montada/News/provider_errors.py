"""
Error responses for calls to third-party data providers (ForexNewsAPI, Marketaux, ...).

A provider's 401/403 means *our server's* API key was rejected, not the app user's JWT.
Forwarding that status makes the Flutter app think the user's session expired, so it is
reported as 503 (service unavailable) instead, and the real cause is logged.
"""

import json
import logging

from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

_KEY_REJECTED = (401, 403)


def read_provider_error(exc):
    """Body of an ``urllib.error.HTTPError`` as a dict (reads the stream once)."""
    body = exc.read().decode(errors="replace") if exc.fp else "{}"
    try:
        data = json.loads(body)
    except Exception:
        data = {"error": body or str(exc)}
    return data if isinstance(data, dict) else {"error": str(data)}


def provider_error_response(exc, provider="News provider", err_data=None):
    """
    Response for an ``HTTPError`` raised by a provider call.

    Pass ``err_data`` when the body was already read with ``read_provider_error``.
    """
    if err_data is None:
        err_data = read_provider_error(exc)
    if exc.code in _KEY_REJECTED:
        logger.error(
            "%s rejected our API key (HTTP %s, url=%s): %s",
            provider, exc.code, (exc.url or "").split("?")[0], err_data,
        )
        return Response(
            {
                "error": f"{provider} is temporarily unavailable.",
                "message": "The server could not authenticate with the data provider (API key invalid, expired or blocked).",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(err_data, status=exc.code)
