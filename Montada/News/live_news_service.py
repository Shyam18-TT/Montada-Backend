import importlib
import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

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
    # or body text is available, since provider titles can remain in English
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


def _stable_bigint(value):
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    digest = hashlib.blake2b(cleaned.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _with_rss_defaults(payload, *, provider_slug, news_type=None, channel=None):
    if not isinstance(payload, dict):
        return payload
    normalized_provider = str(provider_slug or "rss").strip().lower().replace(" ", "_")
    merged = dict(payload)
    merged.setdefault("_provider_slug", normalized_provider)
    merged.setdefault("_news_type", news_type or f"{normalized_provider}_rss")
    merged.setdefault("_channel", channel or normalized_provider)
    return merged


class _OpenGraphImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image_url = None

    def handle_starttag(self, tag, attrs):
        if self.image_url or str(tag).lower() != "meta":
            return

        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        prop = attr_map.get("property", "").strip().lower()
        name = attr_map.get("name", "").strip().lower()
        if prop != "og:image" and name != "og:image":
            return

        content = attr_map.get("content", "").strip()
        if content:
            self.image_url = content


class _ArticleMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image_url = None
        self.tags = []
        self.json_ld_blocks = []
        self._inside_json_ld = False
        self._json_ld_buffer = []

    def handle_starttag(self, tag, attrs):
        lower_tag = str(tag).lower()
        if lower_tag == "meta":
            attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
            prop = attr_map.get("property", "").strip().lower()
            name = attr_map.get("name", "").strip().lower()
            content = attr_map.get("content", "").strip()

            if content and not self.image_url and (prop == "og:image" or name == "og:image"):
                self.image_url = content

            if not content:
                return

            if name in {"keywords", "news_keywords"}:
                self.tags.extend(
                    [item.strip() for item in content.split(",") if item.strip()]
                )
            elif prop == "article:tag":
                self.tags.append(content)
            return

        if lower_tag == "script":
            attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
            if attr_map.get("type", "").strip().lower() == "application/ld+json":
                self._inside_json_ld = True
                self._json_ld_buffer = []

    def handle_endtag(self, tag):
        if self._inside_json_ld and str(tag).lower() == "script":
            payload = "".join(self._json_ld_buffer).strip()
            if payload:
                self.json_ld_blocks.append(payload)
            self._inside_json_ld = False
            self._json_ld_buffer = []

    def handle_data(self, data):
        if self._inside_json_ld:
            self._json_ld_buffer.append(data)


def fetch_open_graph_image_url(url, *, timeout=15):
    cleaned_url = str(url or "").strip()
    if not cleaned_url:
        return None

    req = urllib.request.Request(cleaned_url, method="GET")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    req.add_header("Accept", "text/html,application/xhtml+xml")

    try:
        with _open_url_with_ssl_fallback(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        logger.exception("Failed to fetch OG image from article url=%s", cleaned_url)
        return None

    parser = _OpenGraphImageParser()
    try:
        parser.feed(body)
    except Exception:
        logger.exception("Failed to parse OG image from article url=%s", cleaned_url)
        return None
    return parser.image_url


def _dedupe_preserve_order(values):
    seen = set()
    result = []
    for value in values or []:
        cleaned = unescape(str(value or "")).replace("\xa0", " ").strip()
        lowered = cleaned.lower()
        if cleaned and lowered not in seen:
            seen.add(lowered)
            result.append(cleaned)
    return result


def _normalize_html_fragment(value):
    if not value:
        return None
    normalized = unescape(str(value)).replace("\xa0", " ").strip()
    return normalized or None


def _extract_newsarticle_from_json_ld(payload):
    if isinstance(payload, dict):
        type_value = payload.get("@type")
        types = type_value if isinstance(type_value, list) else [type_value]
        normalized_types = {str(item).lower() for item in types if item}
        if "newsarticle" in normalized_types or "article" in normalized_types:
            return payload
        for value in payload.values():
            match = _extract_newsarticle_from_json_ld(value)
            if match:
                return match
    elif isinstance(payload, list):
        for item in payload:
            match = _extract_newsarticle_from_json_ld(item)
            if match:
                return match
    return None


def _extract_json_ld_article_details(json_ld_blocks):
    details = {
        "body": None,
        "image_url": None,
        "tags": [],
    }
    for block in json_ld_blocks or []:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue

        article = _extract_newsarticle_from_json_ld(payload)
        if not article:
            continue

        image = article.get("image")
        if isinstance(image, list) and image:
            details["image_url"] = details["image_url"] or str(image[0] or "").strip()
        elif isinstance(image, dict):
            details["image_url"] = details["image_url"] or str(image.get("url") or "").strip()
        elif image:
            details["image_url"] = details["image_url"] or str(image).strip()

        keywords = article.get("keywords")
        if isinstance(keywords, str):
            details["tags"].extend([item.strip() for item in keywords.split(",") if item.strip()])
        elif isinstance(keywords, list):
            details["tags"].extend([str(item or "").strip() for item in keywords if str(item or "").strip()])

        article_section = article.get("articleSection")
        if isinstance(article_section, str) and article_section.strip():
            details["tags"].append(article_section.strip())

        article_body = article.get("articleBody")
        if article_body and not details["body"]:
            body_text = _normalize_html_fragment(article_body)
            if body_text:
                details["body"] = "<p>%s</p>" % body_text.replace("\n", "</p><p>")
    details["tags"] = _dedupe_preserve_order(details["tags"])
    return details


def _extract_article_body_html(page_html):
    if not page_html:
        return None

    article_match = re.search(r"(<article\b[^>]*>.*?</article>)", page_html, re.S | re.I)
    article_html = article_match.group(1) if article_match else page_html
    block_pattern = re.compile(
        r"(<(?:p|h2|h3|figure|blockquote|ul|ol)\b[^>]*>.*?</(?:p|h2|h3|figure|blockquote|ul|ol)>)",
        re.S | re.I,
    )
    blocks = []
    for block in block_pattern.findall(article_html):
        text = _strip_markup(block).strip().lower()
        if not text:
            continue
        if text in {"author", "related news", "read also", "read next"}:
            continue
        if "add fxstreet as preferred source on google" in text:
            continue
        blocks.append(block.strip())

    if not blocks:
        return None
    return _normalize_html_fragment("\n\n".join(blocks))


def fetch_rss_article_details(url, *, timeout=15):
    cleaned_url = str(url or "").strip()
    if not cleaned_url:
        return {
            "body": None,
            "image_url": None,
            "tags": [],
        }

    req = urllib.request.Request(cleaned_url, method="GET")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    req.add_header("Accept", "text/html,application/xhtml+xml")

    try:
        with _open_url_with_ssl_fallback(req, timeout=timeout) as resp:
            page_html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        logger.exception("Failed to fetch RSS article details from url=%s", cleaned_url)
        return {
            "body": None,
            "image_url": None,
            "tags": [],
        }

    parser = _ArticleMetadataParser()
    try:
        parser.feed(page_html)
    except Exception:
        logger.exception("Failed to parse RSS article details from url=%s", cleaned_url)

    json_ld_details = _extract_json_ld_article_details(parser.json_ld_blocks)
    body_html = _extract_article_body_html(page_html) or json_ld_details.get("body")
    body_html = _normalize_html_fragment(body_html)
    image_url = parser.image_url or json_ld_details.get("image_url")
    tags = _dedupe_preserve_order(list(parser.tags) + list(json_ld_details.get("tags") or []))
    return {
        "body": body_html,
        "image_url": image_url,
        "tags": tags,
    }


def normalize_rss_payload(payload):
    if not isinstance(payload, dict):
        return None

    provider_slug = str(payload.get("_provider_slug") or payload.get("provider") or "rss").strip().lower().replace(" ", "_")
    news_type = str(payload.get("_news_type") or f"{provider_slug}_rss").strip() or f"{provider_slug}_rss"
    channel_name = str(payload.get("_channel") or provider_slug).strip() or provider_slug
    guid = payload.get("guid") or payload.get("id")
    source_url = payload.get("link") or payload.get("url")
    title = (payload.get("title") or "").strip()
    teaser = payload.get("description") or payload.get("summary") or payload.get("encoded") or ""
    published_at = _parse_dt(payload.get("pubDate") or payload.get("published"))

    fallback_key = f"{title}|{payload.get('pubDate') or ''}"
    provider_key = f"{provider_slug}|{guid or source_url or fallback_key}"
    provider_content_id = _stable_bigint(provider_key)
    if provider_content_id is None:
        return None

    categories = payload.get("categories") or payload.get("category") or []
    if not isinstance(categories, list):
        categories = [categories] if categories else []

    author = payload.get("author") or payload.get("creator")
    authors = [author] if author else []

    normalized = {
        "provider_event_id": None,
        "provider_content_id": provider_content_id,
        "provider_revision_id": None,
        "original_id": None,
        "action": "Created",
        "news_type": news_type,
        "language": detect_news_language(title, teaser, None),
        "title": title,
        "teaser": teaser,
        "body": None,
        "source_url": source_url,
        "authors": authors,
        "tags": categories,
        "securities": [],
        "channels": [channel_name],
        "images": [],
        "primary_image_url": None,
        "source_created_at": published_at,
        "source_updated_at": published_at,
        "source_timestamp": published_at,
        "is_active": True,
    }
    if not normalized["title"] and not normalized["teaser"]:
        return None
    return normalized


def normalize_fxstreet_payload(payload):
    return normalize_rss_payload(_with_rss_defaults(payload, provider_slug="fxstreet"))


def _enrich_rss_details(normalized, existing=None):
    if not isinstance(normalized, dict):
        return normalized
    if not str(normalized.get("news_type") or "").endswith("_rss"):
        return normalized
    details = fetch_rss_article_details(normalized.get("source_url"))

    image_url = details.get("image_url")
    if image_url:
        normalized["primary_image_url"] = image_url
        normalized["images"] = [{"size": "og", "url": image_url}]
    elif existing and existing.primary_image_url:
        normalized["primary_image_url"] = existing.primary_image_url
        normalized["images"] = existing.images or []

    body = details.get("body")
    if body:
        normalized["body"] = body
    elif existing and existing.body:
        normalized["body"] = existing.body

    merged_tags = list(normalized.get("tags") or [])
    if existing:
        merged_tags.extend(existing.tags or [])
    merged_tags.extend(details.get("tags") or [])
    normalized["tags"] = _dedupe_preserve_order(merged_tags)
    normalized["language"] = detect_news_language(
        normalized.get("title"),
        normalized.get("teaser"),
        normalized.get("body"),
    )
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


def _rss_feed_identity_fields():
    return (
        "action",
        "news_type",
        "title",
        "teaser",
        "source_url",
        "authors",
        "channels",
        "source_created_at",
        "source_updated_at",
        "source_timestamp",
        "is_active",
    )


def _rss_requires_detail_refresh(existing, normalized):
    if not existing or not isinstance(normalized, dict):
        return True
    if not str(normalized.get("news_type") or "").endswith("_rss"):
        return False
    if not existing.body or not existing.primary_image_url:
        return True

    new_updated = normalized.get("source_updated_at") or normalized.get("source_timestamp")
    old_updated = existing.source_updated_at or existing.source_timestamp
    if new_updated and old_updated and new_updated > old_updated:
        return True

    for field in _rss_feed_identity_fields():
        if getattr(existing, field) != normalized.get(field):
            return True
    return False


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
    normalized = normalize_rss_payload(payload)
    if not normalized:
        return None, False, False

    existing = LiveNews.objects.filter(
        provider_content_id=normalized["provider_content_id"]
    ).first()
    if not existing and normalized.get("source_url"):
        existing = LiveNews.objects.filter(source_url=normalized["source_url"]).first()
        if existing:
            normalized["provider_content_id"] = existing.provider_content_id
    should_refresh_rss_details = _rss_requires_detail_refresh(existing, normalized)
    if should_refresh_rss_details:
        normalized = _enrich_rss_details(normalized, existing=existing)
    elif existing:
        return existing, False, False
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


def _xml_local_name(tag):
    return str(tag or "").split("}", 1)[-1]


def _open_url_with_ssl_fallback(request, *, timeout):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            logger.warning(
                "Retrying URL without SSL verification because certificate validation failed: %s",
                getattr(request, "full_url", ""),
            )
            insecure_context = ssl._create_unverified_context()
            return urllib.request.urlopen(request, timeout=timeout, context=insecure_context)
        raise


def _extract_rss_items_with_regex(raw_text):
    items = []
    item_blocks = re.findall(r"<item\b[^>]*>(.*?)</item>", raw_text or "", re.S | re.I)
    field_patterns = {
        "guid": r"<guid\b[^>]*>(.*?)</guid>",
        "link": r"<link\b[^>]*>(.*?)</link>",
        "title": r"<title\b[^>]*>(.*?)</title>",
        "description": r"<description\b[^>]*>(.*?)</description>",
        "pubDate": r"<pubDate\b[^>]*>(.*?)</pubDate>",
        "author": r"<(?:dc:creator|author)\b[^>]*>(.*?)</(?:dc:creator|author)>",
    }

    def _clean_value(value):
        cleaned = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value or "", flags=re.S)
        return unescape(cleaned).strip()

    for block in item_blocks:
        data = {"categories": []}
        for field_name, pattern in field_patterns.items():
            match = re.search(pattern, block, re.S | re.I)
            if match:
                data[field_name] = _clean_value(match.group(1))

        for category in re.findall(r"<category\b[^>]*>(.*?)</category>", block, re.S | re.I):
            cleaned = _clean_value(category)
            if cleaned:
                data["categories"].append(cleaned)

        if data.get("title") or data.get("description"):
            items.append(data)
    return items


def _extract_rss_items(raw_text):
    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError:
        return _extract_rss_items_with_regex(raw_text)

    channel = None
    for child in list(root):
        if _xml_local_name(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        return []

    items = []
    for item in list(channel):
        if _xml_local_name(item.tag) != "item":
            continue

        data = {"categories": []}
        for child in list(item):
            tag = _xml_local_name(child.tag)
            text = (child.text or "").strip()
            if tag == "category":
                if text:
                    data["categories"].append(text)
                continue
            if tag == "creator" and text and not data.get("author"):
                data["author"] = text
                continue
            data[tag] = text

        if data.get("title") or data.get("description"):
            items.append(data)
    return items


def fetch_fxstreet_rss_items(*, feed_url=None, timeout=20):
    configured = feed_url or getattr(
        settings,
        "FXSTREET_RSS_NEWS_URL",
        "https://www.fxstreet.com/rss/news",
    )
    urls = [configured] if isinstance(configured, str) else list(configured or [])
    urls.append("https://www.fxstreet.com/news/feed")
    return fetch_rss_items(
        feed_url=urls,
        provider_slug="fxstreet",
        news_type="fxstreet_rss",
        channel="fxstreet",
        timeout=timeout,
    )


def fetch_fxstreet_arabic_rss_items(*, feed_url=None, timeout=20):
    configured = feed_url or getattr(
        settings,
        "FXSTREET_ARABIC_RSS_NEWS_URL",
        "https://ar.fxstreet.com/rss/news",
    )
    urls = [configured] if isinstance(configured, str) else list(configured or [])
    urls.append("https://ar.fxstreet.com/news/feed")
    return fetch_rss_items(
        feed_url=urls,
        provider_slug="fxstreet_ar",
        news_type="fxstreet_ar_rss",
        channel="fxstreet_ar",
        timeout=timeout,
    )


def fetch_fxstreet_chinese_rss_items(*, feed_url=None, timeout=20):
    configured = feed_url or getattr(
        settings,
        "FXSTREET_CHINESE_RSS_NEWS_URL",
        "https://www.fxstreet.hk/rss/news",
    )
    urls = [configured] if isinstance(configured, str) else list(configured or [])
    urls.append("https://www.fxstreet.hk/news/feed")
    return fetch_rss_items(
        feed_url=urls,
        provider_slug="fxstreet_zh",
        news_type="fxstreet_zh_rss",
        channel="fxstreet_zh",
        timeout=timeout,
    )


def fetch_dailyforex_rss_items(*, feed_url=None, timeout=20):
    url = feed_url or getattr(
        settings,
        "DAILYFOREX_RSS_NEWS_URL",
        "https://www.dailyforex.com/rss/forexnews.xml",
    )
    return fetch_rss_items(
        feed_url=[url],
        provider_slug="dailyforex",
        news_type="dailyforex_rss",
        channel="dailyforex",
        timeout=timeout,
    )


def fetch_forexlive_rss_items(*, feed_url=None, timeout=20):
    url = feed_url or getattr(
        settings,
        "FOREXLIVE_RSS_NEWS_URL",
        "https://www.forexlive.com/feed/",
    )
    return fetch_rss_items(
        feed_url=[url],
        provider_slug="forexlive",
        news_type="forexlive_rss",
        channel="forexlive",
        timeout=timeout,
    )


def fetch_rss_items(*, feed_url, provider_slug, news_type=None, channel=None, timeout=20):
    feed_urls = feed_url if isinstance(feed_url, (list, tuple)) else [feed_url]
    errors = []
    body = None
    for candidate_url in [str(url or "").strip() for url in feed_urls if str(url or "").strip()]:
        req = urllib.request.Request(candidate_url, method="GET")
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        req.add_header("Accept", "application/rss+xml, application/xml, text/xml")
        req.add_header("Referer", candidate_url)
        req.add_header("Accept-Language", "en-US,en;q=0.9,ar;q=0.8,zh-CN;q=0.7")
        try:
            with _open_url_with_ssl_fallback(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                break
        except Exception as exc:
            errors.append(f"{candidate_url}: {exc}")
            logger.warning(
                "RSS fetch failed for provider=%s url=%s error=%s",
                provider_slug,
                candidate_url,
                exc,
            )
    if body is None:
        raise RuntimeError(
            "All RSS feed URLs failed for provider=%s. %s"
            % (provider_slug, " | ".join(errors))
        )
    return [
        _with_rss_defaults(
            item,
            provider_slug=provider_slug,
            news_type=news_type,
            channel=channel,
        )
        for item in _extract_rss_items(body)
    ]
