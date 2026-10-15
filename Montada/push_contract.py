"""
Push notification contract shared by every FCM producer.

The released Flutter app depends on these exact channel ids, sound names, ``data.type``
values and data keys. Producers must not hard-code any of them: they pass ``data["type"]``
(and ``importance`` for economic events) and ``firebase.py`` builds the FCM message from
this module.

Android channels: the app creates every channel at launch. The ``_v2`` ids carry the
``montada_push`` sound; the pre-``_v2`` ids no longer exist in the app and must never be sent.
Sounds: the app ships exactly one sound, ``montada_push`` (Android raw resource, no
extension) and ``montada_push.wav`` (iOS). News plays no sound.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
ANDROID_PACKAGE = "com.app.montada"
IOS_BUNDLE_ID = "com.montada.tclimited"

ANDROID_SOUND = "montada_push"
IOS_SOUND = "montada_push.wav"

DEFAULT_CHANNEL = "montada_notifications"
SILENT_SUFFIX = "_silent"

# data.type values the app understands.
TYPE_USER_PRICE_ALERT = "user_price_alert"
TYPE_MARKET_PERCENTAGE_ALERT = "market_percentage_alert"
TYPE_SYMBOL_MOVE_LEGACY = "signal_change_threshold"  # still accepted by the app; send MARKET_PERCENTAGE_ALERT
TYPE_ECONOMIC_EVENT = "economic_event"
TYPE_NEWS = "news_update"
TYPE_CHAT = "chat_message"
TYPE_BROADCAST = "admin_broadcast"

# FCM rejects data payloads over 4 KB; stay below it.
MAX_DATA_BYTES = 3800

HOUR = 3600


@dataclass(frozen=True)
class ChannelSpec:
    channel: str            # Android channel id used when the push plays a sound
    silent_channel: str     # Android channel id used in "silent" mode
    thread_id: str          # iOS aps.thread-id
    ttl_seconds: int
    plays_sound: bool = True


_PRICE_ALERTS = ChannelSpec("montada_price_alerts_v2", "montada_price_alerts_silent", "price_alerts", 1 * HOUR)
_TRADE_IDEAS = ChannelSpec("montada_trade_ideas_v2", "montada_trade_ideas_silent", "trade_ideas", 1 * HOUR)
_ECONOMIC = ChannelSpec("montada_economic_v2", "montada_economic_silent", "economic", 6 * HOUR)
_ECONOMIC_HIGH = ChannelSpec("montada_economic_high_v2", "montada_economic_high_silent", "economic", 6 * HOUR)
_REMINDERS = ChannelSpec(
    "montada_economic_reminders_v2", "montada_economic_reminders_silent", "reminders", 1 * HOUR,
)
_NEWS = ChannelSpec("montada_news", "montada_news_silent", "news", 6 * HOUR, plays_sound=False)
_CHAT = ChannelSpec("montada_chat", "montada_chat_silent", "chat", 24 * HOUR)
_BROADCASTS = ChannelSpec("montada_broadcasts", "montada_broadcasts_silent", "broadcasts", 72 * HOUR)
# Anything the contract does not list: the app's default channel.
_DEFAULT = ChannelSpec(DEFAULT_CHANNEL, DEFAULT_CHANNEL + SILENT_SUFFIX, "general", 6 * HOUR)

_SPEC_BY_TYPE = {
    TYPE_USER_PRICE_ALERT: _PRICE_ALERTS,
    TYPE_MARKET_PERCENTAGE_ALERT: _PRICE_ALERTS,
    TYPE_SYMBOL_MOVE_LEGACY: _PRICE_ALERTS,
    "signal_published": _TRADE_IDEAS,
    "signal_closed": _TRADE_IDEAS,
    "signal_alert": _TRADE_IDEAS,
    "new_signal": _TRADE_IDEAS,
    "signal_update": _TRADE_IDEAS,
    "economic_reminder": _REMINDERS,
    "economic_global_reminder": _REMINDERS,
    TYPE_NEWS: _NEWS,
    TYPE_CHAT: _CHAT,
    TYPE_BROADCAST: _BROADCASTS,
}


def _clean_type(data: Optional[dict]) -> str:
    return str((data or {}).get("type") or "").strip().lower()


def channel_spec_for(data: Optional[dict]) -> ChannelSpec:
    """ChannelSpec for a push payload (economic events depend on ``importance``)."""
    push_type = _clean_type(data)
    if push_type == TYPE_ECONOMIC_EVENT:
        importance = str((data or {}).get("importance") or "").strip().lower()
        return _ECONOMIC_HIGH if importance == "high" else _ECONOMIC
    return _SPEC_BY_TYPE.get(push_type, _DEFAULT)


def android_channel_for(data: Optional[dict], sound: bool = True) -> str:
    spec = channel_spec_for(data)
    return spec.channel if sound else spec.silent_channel


def android_sound_for(data: Optional[dict], sound: bool = True) -> Optional[str]:
    """Android ``sound`` value, or None when the push must be silent."""
    spec = channel_spec_for(data)
    return ANDROID_SOUND if (sound and spec.plays_sound) else None


def ios_sound_for(data: Optional[dict], sound: bool = True) -> Optional[str]:
    spec = channel_spec_for(data)
    return IOS_SOUND if (sound and spec.plays_sound) else None


def ttl_seconds_for(data: Optional[dict]) -> int:
    return channel_spec_for(data).ttl_seconds


def thread_id_for(data: Optional[dict]) -> str:
    return channel_spec_for(data).thread_id


# ---------------------------------------------------------------------------
# data payload
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_notification_id() -> str:
    return str(uuid.uuid4())


def build_data(
    data: Optional[dict],
    *,
    title: str,
    body: str,
    sound: bool = True,
    notification_id: Optional[str] = None,
    preference_category: Optional[str] = None,
) -> dict:
    """
    Final FCM ``data`` map: every value a string, always carrying the fields the app reads
    (type, notification_id, preference_category, title, body, silent, sent_at).
    Producer-supplied keys are kept as given.
    """
    out = {str(k): "" if v is None else str(v) for k, v in (data or {}).items()}
    out["notification_id"] = str(notification_id or out.get("notification_id") or new_notification_id())
    category = preference_category or out.get("preference_category") or out.get("notification_category") or ""
    if category:
        out["preference_category"] = str(category)
    out["title"] = str(title or "")
    out["body"] = str(body or "")
    out["silent"] = "false" if sound else "true"
    out["sent_at"] = out.get("sent_at") or utc_now_iso()
    out["source"] = "montada-app"
    # Lets the app decide whether to play a sound for foreground notifications it draws itself.
    out["sound"] = "1" if sound else "0"
    return _fit_payload(out)


def _payload_bytes(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _fit_payload(data: dict) -> dict:
    """Keep the payload under FCM's 4 KB limit by dropping bulky optional keys, never required ones."""
    if _payload_bytes(data) <= MAX_DATA_BYTES:
        return data
    for key in ("signal_ids", "provider_content_id", "source_url"):
        data.pop(key, None)
        if _payload_bytes(data) <= MAX_DATA_BYTES:
            return data
    for key in ("title", "body"):
        data[key] = data.get(key, "")[:200]
    if _payload_bytes(data) > MAX_DATA_BYTES:
        logger.error(
            "FCM payload still over %d bytes after trimming; keys=%s",
            MAX_DATA_BYTES, sorted(data.keys()),
        )
    return data


# ---------------------------------------------------------------------------
# Sample payloads (used by `manage.py send_test_push`)
# ---------------------------------------------------------------------------
SAMPLE_PAYLOADS = {
    TYPE_USER_PRICE_ALERT: {
        "title": "TEST price alert",
        "body": "Test push - TESTUSD crossed above 1.0000 (now 1.0001).",
        "data": {
            "alert_id": "test", "symbol": "TESTUSD", "condition": "above",
            "current_price": "1.0001", "target_price": "1.0000", "effective_target": "1.0000",
        },
    },
    TYPE_MARKET_PERCENTAGE_ALERT: {
        "title": "TEST market move",
        "body": "Test push - TESTUSD is up 0.5%.",
        "data": {
            "symbol": "TESTUSD", "direction": "up", "crossed_percentage": "0.5",
            "signed_crossed_percentage": "0.5", "current_percentage": "0.52", "ask": "1.0002", "bid": "1.0001",
        },
    },
    "signal_published": {
        "title": "TEST trade idea",
        "body": "Test push - a new BUY signal for TESTUSD.",
        "data": {
            "signal_id": "test", "symbol": "TESTUSD", "direction": "BUY", "timeframe": "H1", "status": "OPEN",
            "analyst_id": "test", "entry_price": "1.0000", "stop_loss": "0.9900", "take_profit": "1.0200",
        },
    },
    TYPE_ECONOMIC_EVENT: {
        "title": "TEST economic event",
        "body": "Test push - TEST event released.",
        "data": {
            "event_id": "test", "event_name": "Test event", "currency_code": "USD", "country_name": "Test",
            "importance": "high", "actual_value": "1", "forecast_value": "1", "previous_value": "1",
        },
    },
    "economic_reminder": {
        "title": "TEST economic reminder",
        "body": "Test push - event starts in 15 minutes.",
        "data": {
            "event_id": "test", "event_name": "Test event", "currency_code": "USD", "country": "Test",
            "importance": "high", "reminder_type": "custom", "time_remaining": "15 minutes",
            "custom_minutes_before": "15",
        },
    },
    TYPE_NEWS: {
        "title": "TEST news",
        "body": "Test push - a news headline.",
        "data": {"news_id": "test", "source_url": "https://example.com", "language": "en"},
    },
    TYPE_CHAT: {
        "title": "TEST sender",
        "body": "Test push - a chat message.",
        "data": {"conversation_id": "test", "sender_id": "test", "sender_name": "TEST sender"},
    },
    TYPE_BROADCAST: {
        "title": "TEST announcement",
        "body": "Test push - an announcement.",
        "data": {"redirect_url": "", "category": "promotional"},
    },
}
