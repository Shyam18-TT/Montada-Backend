"""
Shared "who / when / which timeframe" context for every trade-idea (signal) notification.

`signal_context_data` returns extra FCM data keys (all strings, additive: existing keys are
untouched). `signal_context_text` returns a short human line to append to the body.
"""
from datetime import timezone as dt_timezone

from django.utils import timezone


def signal_analyst_name(signal):
    analyst = getattr(signal, "analyst", None)
    if not analyst:
        return ""
    return (
        getattr(analyst, "name", None)
        or getattr(analyst, "username", None)
        or getattr(analyst, "email", None)
        or "Analyst"
    )


def _created_at(signal):
    created = getattr(signal, "created_at", None)
    if created is None:
        return None
    if timezone.is_naive(created):
        created = timezone.make_aware(created, dt_timezone.utc)
    return created.astimezone(dt_timezone.utc)


def signal_context_data(signal):
    created = _created_at(signal)
    timeframe = getattr(signal, "timeframe", None)
    return {
        "analyst_id": str(signal.analyst_id) if getattr(signal, "analyst_id", None) else "",
        "analyst_name": signal_analyst_name(signal),
        "signal_created_at": created.isoformat() if created else "",
        "timeframe": timeframe.code if timeframe else "",
        "timeframe_name": timeframe.name if timeframe else "",
    }


def signal_context_text(signal, *, include_analyst=True, include_timeframe=True):
    """e.g. "Analyst: Ali | Posted: 30 Oct 2026 14:05 UTC | Timeframe: H1"."""
    parts = []
    name = signal_analyst_name(signal)
    if include_analyst and name:
        parts.append(f"Analyst: {name}")
    created = _created_at(signal)
    if created:
        parts.append("Posted: " + created.strftime("%d %b %Y %H:%M UTC"))
    timeframe = getattr(signal, "timeframe", None)
    if include_timeframe and timeframe:
        parts.append(f"Timeframe: {timeframe.code}")
    return " | ".join(parts)


def with_signal_context(body, signal, **kwargs):
    line = signal_context_text(signal, **kwargs)
    return f"{body}\n{line}" if line else body
