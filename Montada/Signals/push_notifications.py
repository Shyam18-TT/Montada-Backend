"""
Signal-related FCM push notification helpers.

These are the reusable functions to call whenever you want to notify a set
of users about a signal event (new signal posted, signal closed, etc.).

All functions accept either:
  - users   : iterable / queryset of User instances, OR
  - tokens  : explicit list of FCM token strings

If both are omitted the call is a no-op and returns an empty result dict.

Example
-------
from Signals.push_notifications import (
    notify_new_signal,
    notify_signal_closed,
    notify_signal_update,
    push_signal_notification,
)

# Notify all followers when a new signal is posted
notify_new_signal(
    users=follower_users_qs,
    signal=signal_instance,
)

# Or low-level: custom title / body / image to explicit tokens
push_signal_notification(
    tokens=["tok1", "tok2"],
    title="Custom Title",
    body="Custom body text.",
    data={"key": "value"},
    image_url="https://cdn.example.com/banner.png",
)
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal: import the two core firebase helpers
# ---------------------------------------------------------------------------

try:
    from firebase import send_push_to_tokens, send_push_to_users
    _FIREBASE_AVAILABLE = True
except Exception as _exc:
    logger.warning("firebase module unavailable – push notifications disabled: %s", _exc)
    _FIREBASE_AVAILABLE = False


def _noop_result():
    return {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}


# ---------------------------------------------------------------------------
# Generic dispatcher
# ---------------------------------------------------------------------------

def push_signal_notification(
    title: str,
    body: str,
    users=None,
    tokens: Optional[list] = None,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> dict:
    """
    Send a push notification for any signal event.

    You must supply at least one of `users` or `tokens`.

    Parameters
    ----------
    title     : notification title.
    body      : notification body text.
    users     : queryset / iterable of User instances whose devices to notify.
    tokens    : explicit list of FCM token strings (takes precedence over users
                when both are provided).
    data      : optional dict of extra string key-value pairs (data payload).
    image_url : optional image URL shown inside the notification.

    Returns
    -------
    dict { success_count, failure_count, failed_tokens, errors }
    """
    if not _FIREBASE_AVAILABLE:
        return _noop_result()

    if tokens:
        return send_push_to_tokens(
            tokens=list(tokens),
            title=title,
            body=body,
            data=data,
            image_url=image_url,
        )
    if users is not None:
        return send_push_to_users(
            users=users,
            title=title,
            body=body,
            data=data,
            image_url=image_url,
        )

    logger.warning("push_signal_notification called with neither users nor tokens – skipped.")
    return _noop_result()


# ---------------------------------------------------------------------------
# Signal-specific convenience helpers
# ---------------------------------------------------------------------------

def notify_new_signal(signal, users=None, tokens=None) -> dict:
    """
    Notify users that a new trading signal has been posted.

    Parameters
    ----------
    signal : TradingSignal instance.
    users  : recipients (User queryset / list).
    tokens : explicit FCM tokens (alternative to users).
    """
    instrument = getattr(signal.instrument, "symbol", None) if signal.instrument else None
    asset_class = getattr(signal.asset_class, "name", None) if signal.asset_class else None
    direction = getattr(signal, "direction", "")

    title = "New Signal Posted"
    parts = [p for p in [instrument, direction] if p]
    body = f"New {asset_class or ''} signal: {' | '.join(parts)}".strip(" :")

    data = {
        "type": "new_signal",
        "signal_id": str(signal.id),
        "direction": str(direction),
    }
    if instrument:
        data["instrument"] = instrument
    if asset_class:
        data["asset_class"] = asset_class

    image_url = _signal_image(signal)
    return push_signal_notification(
        title=title,
        body=body,
        users=users,
        tokens=tokens,
        data=data,
        image_url=image_url,
    )


def notify_signal_closed(signal, users=None, tokens=None) -> dict:
    """
    Notify users that a signal has been closed (win / loss / neutral).

    Parameters
    ----------
    signal : TradingSignal instance (is_win / is_loss / is_neutral already set).
    users  : recipients.
    tokens : explicit FCM tokens (alternative to users).
    """
    instrument = getattr(signal.instrument, "symbol", None) if signal.instrument else None
    direction = getattr(signal, "direction", "")

    if signal.is_win:
        outcome = "WIN"
        emoji = "✅"
    elif signal.is_loss:
        outcome = "LOSS"
        emoji = "❌"
    else:
        outcome = "NEUTRAL"
        emoji = "➖"

    title = f"Signal Closed – {outcome} {emoji}"
    parts = [p for p in [instrument, direction] if p]
    body = f"{'  |  '.join(parts)} signal has been closed as {outcome.lower()}."

    data = {
        "type": "signal_closed",
        "signal_id": str(signal.id),
        "outcome": outcome.lower(),
    }
    if instrument:
        data["instrument"] = instrument

    image_url = _signal_image(signal)
    return push_signal_notification(
        title=title,
        body=body,
        users=users,
        tokens=tokens,
        data=data,
        image_url=image_url,
    )


def notify_signal_update(signal, update_message: str, users=None, tokens=None) -> dict:
    """
    Notify users of a generic update to a signal (e.g. price level changed).

    Parameters
    ----------
    signal         : TradingSignal instance.
    update_message : short description of what changed.
    users          : recipients.
    tokens         : explicit FCM tokens (alternative to users).
    """
    instrument = getattr(signal.instrument, "symbol", None) if signal.instrument else None

    title = f"Signal Updated{': ' + instrument if instrument else ''}"
    body = update_message

    data = {
        "type": "signal_update",
        "signal_id": str(signal.id),
        "message": update_message,
    }
    if instrument:
        data["instrument"] = instrument

    image_url = _signal_image(signal)
    return push_signal_notification(
        title=title,
        body=body,
        users=users,
        tokens=tokens,
        data=data,
        image_url=image_url,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _signal_image(signal) -> Optional[str]:
    """
    Return an absolute image URL for a signal if one is attached.
    Currently checks signal.instrument.icon (adjust field name as needed).
    Returns None when no image is available.
    """
    try:
        from django.conf import settings

        base = getattr(settings, "PUBLIC_MEDIA_BASE_URL", "").rstrip("/")
        icon = getattr(signal.instrument, "icon", None) if signal.instrument else None
        if icon and hasattr(icon, "url"):
            return f"{base}{icon.url}" if base else icon.url
    except Exception:
        pass
    return None
