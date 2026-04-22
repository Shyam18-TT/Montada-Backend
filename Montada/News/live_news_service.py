import importlib
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils.dateparse import parse_datetime

from .models import LiveNews

try:
    langid = importlib.import_module("langid")
except ImportError:  # pragma: no cover - fallback for environments missing the dependency.
    langid = None


logger = logging.getLogger(__name__)

LIVE_NEWS_GROUP_NAME = "live_news_stream"
FRONTEND_LIVE_NEWS_LANGUAGES = {"ar", "en", "zh"}


def _parse_dt(value):
    if not value:
        return None
    if not isinstance(value, str):
        return value
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


def _normalize_list(value):
    return value if isinstance(value, list) else []


def _strip_markup(value):
    if not value:
        return ""
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _word_count(text):
    if not text:
        return 0
    return len(re.findall(r"[A-Za-z\u00C0-\u024F]+", text))


def _count_language_markers(text):
    arabic_count = 0
    latin_count = 0
    korean_count = 0
    cjk_count = 0
    japanese_kana_count = 0
    for char in text:
        code = ord(char)
        if (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0x08A0 <= code <= 0x08FF
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
        ):
            arabic_count += 1
        elif 0xAC00 <= code <= 0xD7AF:
            korean_count += 1
        elif 0x3040 <= code <= 0x309F or 0x30A0 <= code <= 0x30FF:
            japanese_kana_count += 1
        elif 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            cjk_count += 1
        elif ("A" <= char <= "Z") or ("a" <= char <= "z"):
            latin_count += 1
    return arabic_count, latin_count, korean_count, cjk_count, japanese_kana_count


def _heuristic_language_code(text):
    text = _strip_markup(text)
    if not text:
        return "unknown"

    arabic_count, latin_count, korean_count, cjk_count, japanese_kana_count = _count_language_markers(text)
    if arabic_count >= 3 and arabic_count > latin_count:
        return "ar"
    if korean_count >= 2:
        return "ko"
    if japanese_kana_count >= 2:
        return "ja"
    if cjk_count >= 2:
        return "zh"
    return "unknown"


def _normalize_language_code(language):
    normalized = (language or "").strip().lower().replace("_", "-")
    if not normalized:
        return ""

    primary = normalized.split("-", 1)[0]
    alias_map = {
        "zh-cn": "zh",
        "zh-tw": "zh",
        "zh-hans": "zh",
        "zh-hant": "zh",
        "iw": "he",
    }
    if normalized in alias_map:
        return alias_map[normalized]
    if primary in alias_map:
        return alias_map[primary]
    if re.fullmatch(r"[a-z]{2,3}", primary):
        return primary
    return "unknown"


def _library_detect_language(text):
    if langid is None:
        return None

    cleaned_text = _strip_markup(text)
    if not cleaned_text:
        return None

    try:
        language, _score = langid.classify(cleaned_text)
    except Exception:
        logger.exception("Language detection failed for live news content")
        return None

    normalized_language = _normalize_language_code(language)
    return normalized_language or None


def _detect_text_language(text):
    cleaned_text = _strip_markup(text)
    if not cleaned_text:
        return "unknown"

    heuristic_language = _heuristic_language_code(cleaned_text)
    if heuristic_language in {"ar", "ko", "ja", "zh"}:
        return heuristic_language

    if _word_count(cleaned_text) < 2 and len(cleaned_text) < 12:
        return heuristic_language

    language = _library_detect_language(cleaned_text)
    if language and language != "unknown":
        return language

    return heuristic_language


def _pick_weighted_language(*entries):
    scores = {}
    for language, text, base_weight in entries:
        if language == "unknown":
            continue

        word_count = _word_count(text)
        if word_count < 3:
            continue

        score = min(word_count, 40) * base_weight
        scores[language] = scores.get(language, 0) + score

    if not scores:
        return None

    ranked_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_language, best_score = ranked_scores[0]
    second_score = ranked_scores[1][1] if len(ranked_scores) > 1 else 0
    if best_score > second_score:
        return best_language
    return None


def detect_news_language(title=None, teaser=None, body=None):
    # Detect the language of the actual article content first when enough teaser
    # or body text is available, since Benzinga titles can remain in English
    # even when the article itself is translated.
    title_text = _strip_markup(title)
    teaser_text = _strip_markup(teaser)
    body_text = _strip_markup(body)
    content_text = " ".join(part for part in (teaser_text, body_text) if part).strip()

    title_language = _detect_text_language(title_text)
    teaser_language = _detect_text_language(teaser_text)
    body_language = _detect_text_language(body_text)
    content_language = _detect_text_language(content_text)
    title_words = _word_count(title_text)
    teaser_words = _word_count(teaser_text)
    body_words = _word_count(body_text)
    content_words = _word_count(content_text)

    if content_language != "unknown" and content_words >= 6:
        return content_language

    if body_language != "unknown" and body_words >= 5:
        return body_language

    if teaser_language != "unknown" and teaser_words >= 5:
        return teaser_language

    if (
        body_language != "unknown"
        and body_words >= 5
        and teaser_language == body_language
    ):
        return body_language

    weighted_language = _pick_weighted_language(
        (title_language, title_text, 1),
        (teaser_language, teaser_text, 2),
        (body_language, body_text, 3),
        (content_language, content_text, 2),
    )
    if weighted_language:
        return weighted_language

    if title_language != "unknown":
        return title_language

    if teaser_language != "unknown":
        return teaser_language

    if body_language != "unknown":
        return body_language

    return content_language


def is_supported_live_news_language(language):
    return bool(_normalize_language_code(language))


def is_frontend_live_news_language(language):
    return _normalize_language_code(language) in FRONTEND_LIVE_NEWS_LANGUAGES


def _primary_image_url(images):
    if not isinstance(images, list):
        return None
    preferred_sizes = ("large", "small", "thumb")
    for size in preferred_sizes:
        for image in images:
            if image.get("size") == size and image.get("url"):
                return image["url"]
    for image in images:
        if image.get("url"):
            return image["url"]
    return None


def _content_from_payload(payload):
    if not isinstance(payload, dict):
        return None, None, None, None
    data = payload.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, dict):
            return data, content, data.get("action"), data.get("timestamp")
    return None, payload, payload.get("action"), payload.get("timestamp")


def normalize_benzinga_payload(payload):
    outer_data, content, action, source_timestamp = _content_from_payload(payload)
    if not isinstance(content, dict):
        return None

    provider_content_id = content.get("id") or (outer_data or {}).get("content_id")
    if provider_content_id is None:
        return None

    images = _normalize_list(content.get("image"))
    authors = _normalize_list(content.get("authors"))
    if not authors and content.get("author"):
        authors = [content.get("author")]

    normalized = {
        "provider_event_id": (outer_data or {}).get("id"),
        "provider_content_id": provider_content_id,
        "provider_revision_id": content.get("revision_id"),
        "original_id": content.get("original_id"),
        "action": action or "Created",
        "news_type": content.get("type"),
        "language": detect_news_language(
            content.get("title"),
            content.get("teaser"),
            content.get("body"),
        ),
        "title": content.get("title") or "",
        "teaser": content.get("teaser"),
        "body": content.get("body"),
        "source_url": content.get("url"),
        "authors": authors,
        "tags": _normalize_list(content.get("tags")),
        "securities": _normalize_list(content.get("securities")),
        "channels": _normalize_list(content.get("channels")),
        "images": images,
        "primary_image_url": _primary_image_url(images),
        "source_created_at": _parse_dt(content.get("created_at") or content.get("created")),
        "source_updated_at": _parse_dt(content.get("updated_at") or content.get("updated")),
        "source_timestamp": _parse_dt(source_timestamp),
        "is_active": str(action or "").lower() != "deleted",
    }
    if not normalized["title"] and not normalized["teaser"]:
        return None
    return normalized


def serialize_live_news(instance):
    return {
        "id": str(instance.id),
        "provider_event_id": instance.provider_event_id,
        "provider_content_id": instance.provider_content_id,
        "provider_revision_id": instance.provider_revision_id,
        "original_id": instance.original_id,
        "action": instance.action,
        "news_type": instance.news_type,
        "language": instance.language,
        "title": instance.title,
        "teaser": instance.teaser,
        "body": instance.body,
        "source_url": instance.source_url,
        "authors": instance.authors,
        "tags": instance.tags,
        "securities": instance.securities,
        "channels": instance.channels,
        "images": instance.images,
        "primary_image_url": instance.primary_image_url,
        "source_created_at": instance.source_created_at.isoformat() if instance.source_created_at else None,
        "source_updated_at": instance.source_updated_at.isoformat() if instance.source_updated_at else None,
        "source_timestamp": instance.source_timestamp.isoformat() if instance.source_timestamp else None,
        "is_active": instance.is_active,
        "created_at": instance.created_at.isoformat() if instance.created_at else None,
        "updated_at": instance.updated_at.isoformat() if instance.updated_at else None,
    }


def _significant_live_news_fields():
    return (
        "provider_event_id",
        "provider_revision_id",
        "original_id",
        "action",
        "news_type",
        "language",
        "title",
        "teaser",
        "body",
        "source_url",
        "authors",
        "tags",
        "securities",
        "channels",
        "images",
        "primary_image_url",
        "source_created_at",
        "source_updated_at",
        "source_timestamp",
        "is_active",
    )


def _is_stale_or_unchanged(existing, normalized):
    new_revision = normalized.get("provider_revision_id")
    old_revision = existing.provider_revision_id
    if (
        new_revision is not None
        and old_revision is not None
        and int(new_revision) < int(old_revision)
    ):
        return True

    new_updated = normalized.get("source_updated_at") or normalized.get("source_timestamp")
    old_updated = existing.source_updated_at or existing.source_timestamp
    if new_updated and old_updated and new_updated < old_updated:
        return True

    for field in _significant_live_news_fields():
        if getattr(existing, field) != normalized.get(field):
            return False
    return True


def broadcast_live_news(instance, *, event_name="upsert"):
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning("Live news broadcast skipped: no channel layer configured.")
            return
        async_to_sync(channel_layer.group_send)(
            LIVE_NEWS_GROUP_NAME,
            {
                "type": "news.update",
                "event": event_name,
                "item": serialize_live_news(instance),
            },
        )
    except Exception:
        logger.exception(
            "Live news broadcast failed for provider_content_id=%s",
            instance.provider_content_id,
        )


def save_live_news_payload(payload, *, broadcast=False):
    normalized = normalize_benzinga_payload(payload)
    if not normalized:
        return None, False, False

    existing = LiveNews.objects.filter(
        provider_content_id=normalized["provider_content_id"]
    ).first()
    if existing and _is_stale_or_unchanged(existing, normalized):
        return existing, False, False

    if not is_frontend_live_news_language(normalized.get("language")):
        if not existing:
            return None, False, False

        previously_visible = (
            existing.is_active and is_frontend_live_news_language(existing.language)
        )
        existing.delete()
        if broadcast and previously_visible:
            broadcast_live_news(existing, event_name="deleted")
        return None, False, True

    instance, created = LiveNews.objects.update_or_create(
        provider_content_id=normalized["provider_content_id"],
        defaults=normalized,
    )
    if broadcast:
        current_is_frontend_visible = (
            instance.is_active and is_frontend_live_news_language(instance.language)
        )
        previous_is_frontend_visible = (
            bool(existing)
            and existing.is_active
            and is_frontend_live_news_language(existing.language)
        )
        if current_is_frontend_visible:
            event_name = "created" if created else str(normalized.get("action") or "updated").lower()
            broadcast_live_news(instance, event_name=event_name)
        elif previous_is_frontend_visible:
            broadcast_live_news(instance, event_name="deleted")
    return instance, created, True


def extract_benzinga_rest_items(raw_payload):
    if isinstance(raw_payload, list):
        return raw_payload
    if not isinstance(raw_payload, dict):
        return []
    for key in ("news", "data", "items", "results"):
        value = raw_payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _xml_item_to_dict(item):
    data = {}
    for child in list(item):
        tag = child.tag
        children = list(child)
        if children:
            if tag in {"stocks", "channels", "tags", "authors"}:
                values = []
                for sub in children:
                    text = (sub.text or "").strip()
                    if text:
                        values.append(text)
                data[tag] = values
            else:
                data[tag] = _xml_item_to_dict(child)
        else:
            data[tag] = (child.text or "").strip()
    return data


def _extract_benzinga_xml_items(raw_text):
    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError:
        return []
    if root.tag != "result":
        return []
    return [_xml_item_to_dict(item) for item in root.findall("item")]


def fetch_benzinga_news_page(*, page=0, page_size=50, tickers=None, channels=None):
    token = getattr(settings, "BENZINGA_API_TOKEN", "")
    if not token:
        raise RuntimeError("BENZINGA_API_TOKEN is not configured.")

    base_url = getattr(settings, "BENZINGA_NEWS_URL", "https://api.benzinga.com/api/v2/news")
    params = {
        "token": token,
        "page": str(page),
        "pageSize": str(max(1, min(int(page_size), 100))),
        "displayOutput": "full",
        "sort": "updated:desc",
    }
    if tickers:
        params["tickers"] = tickers
    if channels:
        params["channels"] = channels

    url = base_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode()
        content_type = (resp.headers.get_content_type() or "").lower()
    if "json" in content_type:
        raw = json.loads(body) if body.strip() else []
        return extract_benzinga_rest_items(raw)
    if "xml" in content_type:
        return _extract_benzinga_xml_items(body)
    if not body.strip():
        return []
    try:
        raw = json.loads(body)
        return extract_benzinga_rest_items(raw)
    except json.JSONDecodeError:
        return _extract_benzinga_xml_items(body)


def build_benzinga_stream_url(*, tickers=None, channels=None):
    token = getattr(settings, "BENZINGA_API_TOKEN", "")
    if not token:
        raise RuntimeError("BENZINGA_API_TOKEN is not configured.")

    base_url = getattr(
        settings,
        "BENZINGA_NEWS_STREAM_URL",
        "wss://api.benzinga.com/api/v1/news/stream",
    )
    params = {"token": token}
    if tickers:
        params["tickers"] = tickers
    if channels:
        params["channels"] = channels
    return base_url + "?" + urllib.parse.urlencode(params)
