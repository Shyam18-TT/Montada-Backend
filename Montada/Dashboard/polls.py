"""
Poll scheduling, voting sessions, resets and results.

This module is the single place for poll business rules; the user views (Dashboard/views.py),
the admin views (MontadaAdmin/views.py) and the update_poll_statuses command all call into it.

Model:
- PollQuestion is the poll. Its id, options and responses are stable across resets.
- PollSession is one voting period (schedule + status). PollQuestion.current_session is the latest.
- PollSessionOption snapshots the options offered in a session, so editing options later cannot
  change historical results.
- PollResponse rows belong to exactly one session and are never updated or deleted here.

Statuses: scheduled -> active -> closed, or cancelled. The stored status is maintained by the
update_poll_statuses command, but every read and every vote uses effective_status() / the
*_q() filters, which compare against the schedule directly, so behaviour is correct even if the
scheduler runs late.
"""
import logging
import uuid
from datetime import datetime, timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, F, Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    DEFAULT_POLL_DURATION_HOURS,
    PollOption,
    PollQuestion,
    PollResponse,
    PollSession,
    PollSessionOption,
)

logger = logging.getLogger("polls")

MIN_OPTIONS = 2
MAX_OPTIONS_ON_CREATE = 4
MAX_DURATION_HOURS = 366 * 24
# A start_at this far in the past is treated as "now" (client clock skew); older is rejected.
START_AT_PAST_TOLERANCE = timedelta(minutes=5)
# SQL Server allows ~2100 parameters per statement; keep IN (...) lists well below that.
_ID_CHUNK = 500

SCHEDULED = PollSession.STATUS_SCHEDULED
ACTIVE = PollSession.STATUS_ACTIVE
CLOSED = PollSession.STATUS_CLOSED
CANCELLED = PollSession.STATUS_CANCELLED
OPEN_STATUSES = PollSession.OPEN_STATUSES
STATUS_VALUES = (SCHEDULED, ACTIVE, CLOSED, CANCELLED)


class PollError(Exception):
    """Business-rule violation; views turn it into {"error": message} with status_code."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Effective-status filters (independent of the scheduler)
# ---------------------------------------------------------------------------

def _not_ended_q(prefix, now):
    return Q(**{f"{prefix}end_at__isnull": True}) | Q(**{f"{prefix}end_at__gt": now})


def session_status_q(status, now=None, prefix=""):
    """Q matching sessions whose effective status is `status`. Use prefix="current_session__" on PollQuestion."""
    now = now or timezone.now()
    open_q = Q(**{f"{prefix}status__in": OPEN_STATUSES})
    if status == ACTIVE:
        return open_q & Q(**{f"{prefix}start_at__lte": now}) & _not_ended_q(prefix, now)
    if status == SCHEDULED:
        return open_q & Q(**{f"{prefix}start_at__gt": now}) & _not_ended_q(prefix, now)
    if status == CLOSED:
        return Q(**{f"{prefix}status": CLOSED}) | (open_q & Q(**{f"{prefix}end_at__lte": now}))
    if status == CANCELLED:
        return Q(**{f"{prefix}status": CANCELLED})
    raise ValueError(f"Unknown poll status: {status}")


def question_status_q(status, now=None):
    return session_status_q(status, now, prefix="current_session__")


def question_effective_status(question, now=None):
    session = question.current_session
    return session.effective_status(now) if session else CLOSED


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_uuid(value, field):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise PollError(f"Invalid {field}.")


def parse_datetime_value(value, field):
    """Accept an ISO 8601 string or datetime; naive values are interpreted in the project timezone."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = parse_datetime(str(value).strip())
        except ValueError:
            dt = None
        if dt is None:
            raise PollError(f"{field} must be an ISO 8601 datetime, e.g. 2026-01-31T09:00:00Z.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def parse_duration_hours(data):
    """duration_hours (int) or duration_days (number) from the payload; None if neither given."""
    if data.get("duration_hours") not in (None, ""):
        raw, unit = data.get("duration_hours"), 1
        field = "duration_hours"
    elif data.get("duration_days") not in (None, ""):
        raw, unit = data.get("duration_days"), 24
        field = "duration_days"
    else:
        return None
    try:
        hours = round(float(raw) * unit)
    except (TypeError, ValueError):
        raise PollError(f"{field} must be a positive number.")
    if hours < 1 or hours > MAX_DURATION_HOURS:
        raise PollError(f"{field} must be between 1 hour and {MAX_DURATION_HOURS // 24} days.")
    return hours


def _parse_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _validate_start(start, now):
    if start < now - START_AT_PAST_TOLERANCE:
        raise PollError("start_at cannot be in the past.")
    return max(start, now)


def _validate_window(start, end, now):
    if end <= start:
        raise PollError("end_at must be later than start_at.")
    if end <= now:
        raise PollError("end_at must be in the future.")
    if end - start > timedelta(hours=MAX_DURATION_HOURS):
        raise PollError(f"A poll session cannot last longer than {MAX_DURATION_HOURS // 24} days.")


def resolve_new_schedule(data, duration_hours, now):
    """(start_at, end_at) for a new session: explicit dates win, else start now and run duration_hours."""
    start = parse_datetime_value(data.get("start_at"), "start_at")
    start = _validate_start(start, now) if start else now
    end = parse_datetime_value(data.get("end_at"), "end_at")
    if end is None:
        end = start + timedelta(hours=duration_hours)
    _validate_window(start, end, now)
    return start, end


def parse_option_texts(raw):
    """Option texts from a list of {"option_text": ...} dicts or strings (existing payload format)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PollError("options must be a list of option objects or strings.")
    texts = []
    for item in raw:
        if isinstance(item, dict):
            text = (item.get("option_text") or "").strip()
        elif isinstance(item, str):
            text = item.strip()
        else:
            continue
        if text:
            if len(text) > 255:
                raise PollError("option_text must be at most 255 characters.")
            texts.append(text)
    return texts


def _admin_id(admin):
    return getattr(admin, "pk", None)


def _audit(action, question, admin, **extra):
    details = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info("poll_%s poll=%s admin=%s %s", action, question.pk, _admin_id(admin), details)


# ---------------------------------------------------------------------------
# Locking helpers
# ---------------------------------------------------------------------------

def _lock_question(question_id, nowait=False):
    """Lock the poll row for the rest of the transaction. Must be called inside transaction.atomic()."""
    try:
        return (
            PollQuestion.objects.select_for_update(nowait=nowait)
            .get(pk=question_id, is_deleted=False)
        )
    except PollQuestion.DoesNotExist:
        raise PollError("Poll not found.", 404)
    except DatabaseError:
        if nowait:
            raise PollError("Another change to this poll is in progress. Please try again.", 409)
        raise


def _lock_current_session(question):
    if not question.current_session_id:
        return None
    return PollSession.objects.select_for_update().get(pk=question.current_session_id)


def _live_options(question):
    return list(question.options.filter(is_deleted=False))


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def _sync_session_options(session):
    """
    Make an open session's snapshot match the poll's live options. Snapshot rows that already have
    votes in this session are never removed or renamed, so recorded results cannot change.
    """
    snapshot = {so.option_id: so for so in session.session_options.all()}
    live = {opt.id: opt for opt in _live_options(session.question)}
    voted = set(PollResponse.objects.filter(session=session).values_list("option_id", flat=True).distinct())
    stale = [so.pk for oid, so in snapshot.items() if oid not in live and oid not in voted]
    if stale:
        PollSessionOption.objects.filter(pk__in=stale).delete()
    next_order = max((so.order for so in snapshot.values()), default=-1) + 1
    for oid, opt in live.items():
        so = snapshot.get(oid)
        if so is None:
            PollSessionOption.objects.create(
                session=session, option=opt, option_text=opt.option_text, order=next_order,
            )
            next_order += 1
        elif so.option_text != opt.option_text and oid not in voted:
            so.option_text = opt.option_text
            so.save(update_fields=["option_text"])


def _snapshot_options(session, ordered_options):
    PollSessionOption.objects.bulk_create([
        PollSessionOption(session=session, option=opt, option_text=opt.option_text, order=i)
        for i, opt in enumerate(ordered_options)
    ])


def _ordered_live_options(question, previous_session=None):
    """Live options, keeping the previous session's display order and appending new ones."""
    live = _live_options(question)
    if previous_session is None:
        return live
    order = {so.option_id: so.order for so in previous_session.session_options.all()}
    return sorted(live, key=lambda o: (order.get(o.id, len(order)), str(o.id)))


def _open_session(question, start, end, reason, admin, now, ordered_options):
    number = (question.sessions.aggregate(m=Max("session_number"))["m"] or 0) + 1
    is_active_now = start <= now
    session = PollSession.objects.create(
        question=question,
        session_number=number,
        status=ACTIVE if is_active_now else SCHEDULED,
        start_at=start,
        end_at=end,
        created_at=now,
        created_reason=reason,
        created_by=admin if _admin_id(admin) else None,
        activated_at=now if is_active_now else None,
    )
    _snapshot_options(session, ordered_options)
    question.current_session = session
    question.is_active = is_active_now
    question.save(update_fields=["current_session", "is_active", "updated_at"])
    return session


def _finalize_session(session, now, admin, reason):
    """Close an open session. A session that never started is cancelled; one already past its end keeps end_at."""
    if session.status not in OPEN_STATUSES:
        return
    effective = session.effective_status(now)
    if effective == CLOSED:
        session.status, session.closed_at, session.closed_reason = CLOSED, session.end_at, PollSession.CLOSED_SCHEDULE
        session.closed_by = None
    else:
        # A session that never started and is being replaced/removed/cancelled had no voting period.
        never_started = effective == SCHEDULED and reason != PollSession.CLOSED_MANUAL
        session.status = CANCELLED if (never_started or reason == PollSession.CLOSED_CANCELLED) else CLOSED
        session.closed_at = now
        session.closed_reason = reason
        session.closed_by = admin if _admin_id(admin) else None
    session.save(update_fields=["status", "closed_at", "closed_reason", "closed_by"])


def ensure_current_session(question, admin=None):
    """Give a poll created outside create_poll (e.g. Django admin) its first session."""
    if question.current_session_id:
        return question.current_session
    now = timezone.now()
    with transaction.atomic():
        question = _lock_question(question.pk)
        if question.current_session_id:
            return question.current_session
        publish = question.is_active
        session = _open_session(
            question, now, now + timedelta(hours=question.duration_hours),
            PollSession.REASON_INITIAL, admin, now, _live_options(question),
        )
        if not publish:
            _finalize_session(session, now, admin, PollSession.CLOSED_MANUAL)
            question.is_active = False
            question.save(update_fields=["is_active", "updated_at"])
    _audit("create", question, admin, session=session.pk, source="django_admin")
    return session


def _require_min_options(question):
    if len(_live_options(question)) < MIN_OPTIONS:
        raise PollError(f"Poll must have at least {MIN_OPTIONS} options.")


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------

def create_poll(admin, data):
    question_text = (data.get("question_text") or "").strip()
    if not question_text:
        raise PollError("question_text is required.")
    question_type = (data.get("question_type") or "single").strip().lower()
    if question_type not in ("single", "multiple"):
        raise PollError("question_type must be 'single' or 'multiple'.")
    try:
        order = int(data.get("order", 0))
    except (TypeError, ValueError):
        order = 0
    is_active = _parse_bool(data.get("is_active"), True)
    texts = parse_option_texts(data.get("options"))
    if len(texts) < MIN_OPTIONS or len(texts) > MAX_OPTIONS_ON_CREATE:
        raise PollError("Poll must have between 2 and 4 options (non-empty).")

    now = timezone.now()
    duration = parse_duration_hours(data) or DEFAULT_POLL_DURATION_HOURS
    start, end = resolve_new_schedule(data, duration, now)

    with transaction.atomic():
        question = PollQuestion.objects.create(
            question_text=question_text,
            question_type=question_type,
            order=max(order, 0),
            is_active=False,
            duration_hours=duration,
            created_by=admin if _admin_id(admin) else None,
            created_at=now,
        )
        options = [PollOption.objects.create(question=question, option_text=t) for t in texts]
        session = _open_session(question, start, end, PollSession.REASON_INITIAL, admin, now, options)
        if not is_active:
            # Legacy "create unpublished": the session exists but is closed until an admin activates it.
            _finalize_session(session, now, admin, PollSession.CLOSED_MANUAL)
            question.is_active = False
            question.save(update_fields=["is_active", "updated_at"])
    _audit("create", question, admin, session=session.pk, start_at=start.isoformat(), end_at=end.isoformat())
    return question


def update_poll(admin, question_id, data):
    """Edit text/type/order, the current session's schedule and the options, without touching history."""
    now = timezone.now()
    changes = []
    with transaction.atomic():
        question = _lock_question(question_id)
        session = _lock_current_session(question)
        session_open = session is not None and session.status in OPEN_STATUSES
        voted_option_ids = set(
            PollResponse.objects.filter(session=session).values_list("option_id", flat=True).distinct()
        ) if session else set()

        if "question_text" in data:
            text = (data.get("question_text") or "").strip()
            if not text:
                raise PollError("question_text cannot be empty.")
            question.question_text = text
            changes.append("question_text")
        if "question_type" in data:
            qtype = data.get("question_type")
            if qtype not in ("single", "multiple"):
                raise PollError("question_type must be 'single' or 'multiple'.")
            if qtype != question.question_type and session_open and voted_option_ids:
                raise PollError("question_type cannot be changed after votes were cast in the current session. Reset the poll first.")
            question.question_type = qtype
            changes.append("question_type")
        if "order" in data:
            try:
                question.order = max(int(data["order"]), 0)
            except (TypeError, ValueError):
                raise PollError("order must be an integer.")
            changes.append("order")

        duration = parse_duration_hours(data)
        if duration:
            question.duration_hours = duration
            changes.append("duration_hours")
        question.save()

        if any(k in data for k in ("start_at", "end_at")) or duration:
            _update_session_schedule(session, data, duration, now)
            changes.append("schedule")

        if "options" in data:
            _replace_options(question, session, session_open, data["options"], voted_option_ids)
            changes.append("options")

        _refresh_is_active(question, session, now)
    _audit("update", question, admin, fields=",".join(changes) or "none")
    return question


def _update_session_schedule(session, data, duration, now):
    has_dates = any(k in data for k in ("start_at", "end_at"))
    if session is None or session.effective_status(now) not in OPEN_STATUSES:
        if has_dates:
            raise PollError("The current session is closed. Activate or reset the poll to schedule a new voting period.")
        return  # duration-only change applies to future resets
    start = session.start_at
    if data.get("start_at") not in (None, ""):
        new_start = parse_datetime_value(data["start_at"], "start_at")
        if new_start != session.start_at:
            if session.start_at <= now:
                raise PollError("start_at cannot be changed after the session has started.")
            start = _validate_start(new_start, now)
    end = parse_datetime_value(data.get("end_at"), "end_at")
    if end is None:
        end = start + timedelta(hours=duration) if duration else session.end_at
    if end is None:
        raise PollError("end_at is required.")
    _validate_window(start, end, now)
    session.start_at, session.end_at = start, end
    session.status = ACTIVE if start <= now else SCHEDULED
    if session.status == ACTIVE and session.activated_at is None:
        session.activated_at = now
    session.save(update_fields=["start_at", "end_at", "status", "activated_at"])


def _replace_options(question, session, session_open, payload, voted_option_ids):
    """Existing PUT semantics: items with id are renamed, without id are created, missing ones are removed."""
    if not isinstance(payload, list):
        raise PollError("options must be a list.")
    live = {opt.id: opt for opt in _live_options(question)}
    keep, renames, new_texts = set(), {}, []
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = (item.get("option_text") or "").strip()
        if not text:
            continue
        if len(text) > 255:
            raise PollError("option_text must be at most 255 characters.")
        if item.get("id"):
            oid = parse_uuid(item["id"], "option id")
            if oid not in live:
                raise PollError(f"Option {oid} does not belong to this poll.")
            keep.add(oid)
            if live[oid].option_text != text:
                renames[oid] = text
        else:
            new_texts.append(text)
    removed = set(live) - keep

    if session_open:
        locked = voted_option_ids & (removed | set(renames))
        if locked:
            raise PollError(
                "Options that already have votes in the current session cannot be renamed or removed. "
                "Reset the poll to start a new session."
            )
    if len(keep) + len(new_texts) < MIN_OPTIONS:
        raise PollError(f"Poll must have at least {MIN_OPTIONS} options.")

    for oid, text in renames.items():
        PollOption.objects.filter(pk=oid).update(option_text=text)
    if removed:
        PollOption.objects.filter(pk__in=removed).update(is_deleted=True)
    for text in new_texts:
        PollOption.objects.create(question=question, option_text=text)
    if session_open:
        _sync_session_options(session)


def add_options(admin, question_id, texts):
    if not texts:
        raise PollError("Provide 'option_text' or 'options' (list of { option_text } or strings).")
    with transaction.atomic():
        question = _lock_question(question_id)
        session = _lock_current_session(question)
        created = [PollOption.objects.create(question=question, option_text=t) for t in texts]
        if session is not None and session.status in OPEN_STATUSES:
            _sync_session_options(session)
    _audit("options_add", question, admin, count=len(created))
    return created


def delete_option(admin, question_id, option_id):
    """Soft-delete an option so historical responses and snapshots stay intact."""
    with transaction.atomic():
        question = _lock_question(question_id)
        session = _lock_current_session(question)
        try:
            option = PollOption.objects.get(pk=option_id, question=question, is_deleted=False)
        except PollOption.DoesNotExist:
            raise PollError("Option not found.", 404)
        session_open = session is not None and session.status in OPEN_STATUSES
        if session_open and PollResponse.objects.filter(session=session, option=option).exists():
            raise PollError(
                "This option already has votes in the current session and cannot be removed. "
                "Reset the poll to start a new session."
            )
        if len(_live_options(question)) <= MIN_OPTIONS:
            raise PollError(f"Poll must have at least {MIN_OPTIONS} options.")
        option.is_deleted = True
        option.save(update_fields=["is_deleted"])
        if session_open:
            _sync_session_options(session)
    _audit("option_delete", question, admin, option=option.pk)


def reset_poll(admin, question_id, data):
    """
    Finalize the current session (keeping all its votes) and open a new numbered session.
    Atomic: on any error nothing changes. Concurrent resets get 409 instead of creating two sessions.
    """
    now = timezone.now()
    expected = data.get("current_session_id")
    expected = parse_uuid(expected, "current_session_id") if expected not in (None, "") else None
    with transaction.atomic():
        question = _lock_question(question_id, nowait=True)
        if expected and expected != question.current_session_id:
            raise PollError("This poll was already reset. Refresh and try again.", 409)
        _require_min_options(question)
        duration = parse_duration_hours(data)
        if duration:
            question.duration_hours = duration
            question.save(update_fields=["duration_hours", "updated_at"])
        start, end = resolve_new_schedule(data, question.duration_hours, now)

        previous = _lock_current_session(question)
        if previous is not None:
            _finalize_session(previous, now, admin, PollSession.CLOSED_RESET)
        session = _open_session(
            question, start, end, PollSession.REASON_RESET, admin, now,
            _ordered_live_options(question, previous),
        )
    _audit(
        "reset", question, admin,
        previous_session=getattr(previous, "pk", None), new_session=session.pk,
        start_at=start.isoformat(), end_at=end.isoformat(),
    )
    return question, session


def activate_poll(admin, question_id, data):
    """
    Start a scheduled session now, or reopen a manually closed one.
    A closed session whose end time has passed needs a new future end_at (or a reset).
    """
    now = timezone.now()
    with transaction.atomic():
        question = _lock_question(question_id)
        session = _lock_current_session(question)
        if session is None:
            raise PollError("Poll has no voting session. Reset the poll to create one.")
        if session.status == CANCELLED:
            raise PollError("A cancelled poll cannot be reactivated. Reset the poll to start a new session.")
        _require_min_options(question)
        new_end = parse_datetime_value(data.get("end_at"), "end_at")
        effective = session.effective_status(now)
        if effective == ACTIVE and new_end is None:
            return question, session
        start = min(session.start_at, now)
        end = new_end or session.end_at
        if end is None or end <= now:
            raise PollError("The session end time has passed. Provide a future end_at or reset the poll.")
        _validate_window(start, end, now)
        session.start_at, session.end_at, session.status = start, end, ACTIVE
        session.activated_at = session.activated_at or now
        session.closed_at, session.closed_reason, session.closed_by = None, "", None
        session.save(update_fields=[
            "start_at", "end_at", "status", "activated_at", "closed_at", "closed_reason", "closed_by",
        ])
        _sync_session_options(session)
        question.is_active = True
        question.save(update_fields=["is_active", "updated_at"])
    _audit("activate", question, admin, session=session.pk, previous_status=effective, end_at=end.isoformat())
    return question, session


def close_poll(admin, question_id, cancel=False):
    now = timezone.now()
    reason = PollSession.CLOSED_CANCELLED if cancel else PollSession.CLOSED_MANUAL
    with transaction.atomic():
        question = _lock_question(question_id)
        session = _lock_current_session(question)
        if session is not None:
            if cancel and session.status == CLOSED:
                raise PollError("A closed poll cannot be cancelled.")
            _finalize_session(session, now, admin, reason)
        question.is_active = False
        question.save(update_fields=["is_active", "updated_at"])
    _audit("cancel" if cancel else "close", question, admin, session=getattr(session, "pk", None))
    return question, session


def delete_poll(admin, question_id):
    """Hard-delete only polls that never received votes; otherwise hide the poll and keep its history."""
    now = timezone.now()
    with transaction.atomic():
        question = _lock_question(question_id)
        if not PollResponse.objects.filter(question=question).exists():
            _audit("delete", question, admin, mode="hard")
            question.current_session = None
            question.save(update_fields=["current_session"])
            PollSessionOption.objects.filter(session__question=question).delete()
            question.delete()
            return
        session = _lock_current_session(question)
        if session is not None:
            _finalize_session(session, now, admin, PollSession.CLOSED_DELETED)
        question.is_deleted, question.deleted_at, question.is_active = True, now, False
        question.save(update_fields=["is_deleted", "deleted_at", "is_active", "updated_at"])
    _audit("delete", question, admin, mode="soft")


def _refresh_is_active(question, session, now):
    is_active = bool(session) and session.effective_status(now) == ACTIVE
    if question.is_active != is_active:
        question.is_active = is_active
        question.save(update_fields=["is_active", "updated_at"])


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------

def cast_vote(user, question_id, option_ids):
    """
    Record a vote in the poll's current session. Error messages match the pre-session API.
    The session row is locked for the transaction so a concurrent reset/close/duplicate vote
    cannot interleave; the unique constraint is the final guard.
    """
    if not question_id:
        raise PollError("question_id is required.")
    if not option_ids or not isinstance(option_ids, list):
        raise PollError("option_ids must be a non-empty list.")
    q_uuid = parse_uuid(question_id, "question_id")

    try:
        with transaction.atomic():
            try:
                question = PollQuestion.objects.get(pk=q_uuid, is_deleted=False)
            except PollQuestion.DoesNotExist:
                raise PollError("Question not found.", 404)
            if not question.current_session_id:
                raise PollError("This poll is closed.")
            session = PollSession.objects.select_for_update().get(pk=question.current_session_id)

            effective = session.effective_status()
            if effective == SCHEDULED:
                raise PollError("This poll has not started yet.")
            if effective != ACTIVE:
                raise PollError("This poll is closed.")
            if PollResponse.objects.filter(session=session, user=user).exists():
                raise PollError("You have already voted for this question.")
            if question.question_type == "single" and len(option_ids) != 1:
                raise PollError("This question allows only one option (single choice).")

            valid_option_ids = set(
                session.session_options.filter(option__is_deleted=False).values_list("option_id", flat=True)
            )
            to_create, seen = [], set()
            for oid in option_ids:
                try:
                    o_uuid = parse_uuid(oid, "option_id")
                except PollError:
                    raise PollError(f"Invalid option_id: {oid}.")
                if o_uuid not in valid_option_ids:
                    raise PollError("One or more option_ids are not valid for this question.")
                if o_uuid in seen:
                    continue
                seen.add(o_uuid)
                to_create.append(PollResponse(question=question, session=session, option_id=o_uuid, user=user))
            if not to_create:
                raise PollError("At least one valid option must be provided.")
            PollResponse.objects.bulk_create(to_create)
    except IntegrityError:
        raise PollError("You have already voted for this question.")
    return session


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _pct(count, total):
    return round((count / total) * 100, 2) if total else 0


def vote_counts(session_ids):
    """{session_id: {option_id: count}} in one grouped query."""
    counts = {}
    session_ids = list(session_ids)
    for i in range(0, len(session_ids), _ID_CHUNK):
        rows = (
            PollResponse.objects.filter(session_id__in=session_ids[i:i + _ID_CHUNK])
            .values("session_id", "option_id")
            .annotate(c=Count("id"))
        )
        for row in rows:
            counts.setdefault(row["session_id"], {})[row["option_id"]] = row["c"]
    return counts


def voter_counts(session_ids):
    result = {}
    session_ids = list(session_ids)
    for i in range(0, len(session_ids), _ID_CHUNK):
        rows = (
            PollResponse.objects.filter(session_id__in=session_ids[i:i + _ID_CHUNK])
            .values("session_id")
            .annotate(v=Count("user_id", distinct=True))
        )
        result.update({row["session_id"]: row["v"] for row in rows})
    return result


def session_option_results(session, counts):
    """Per-option results from the session snapshot. total_votes = sum of option counts (existing semantics)."""
    snapshot = sorted(session.session_options.all(), key=lambda so: so.order)
    total = sum(counts.get(so.option_id, 0) for so in snapshot)
    return [
        {
            "id": str(so.option_id),
            "option_text": so.option_text,
            "vote_count": counts.get(so.option_id, 0),
            "vote_percentage": _pct(counts.get(so.option_id, 0), total),
        }
        for so in snapshot
    ], total


def _user_ref(user):
    if user is None:
        return None
    return {"id": str(user.pk), "email": getattr(user, "email", "")}


def _iso(dt):
    return dt.isoformat() if dt else None


def session_to_data(session, counts, voters=None, now=None):
    options, total = session_option_results(session, counts.get(session.pk, {}))
    data = {
        "id": str(session.pk),
        "session_number": session.session_number,
        "status": session.effective_status(now),
        "start_at": _iso(session.start_at),
        "end_at": _iso(session.end_at),
        "created_at": _iso(session.created_at),
        "created_reason": session.created_reason,
        "created_by": _user_ref(session.created_by),
        "activated_at": _iso(session.activated_at),
        "closed_at": _iso(session.closed_at),
        "closed_reason": session.closed_reason or None,
        "closed_by": _user_ref(session.closed_by),
        "total_votes": total,
        "options": options,
    }
    if voters is not None:
        data["total_voters"] = voters.get(session.pk, 0)
    return data


def session_queryset():
    return PollSession.objects.select_related("created_by", "closed_by").prefetch_related("session_options")


def admin_question_queryset(include_deleted=False):
    qs = PollQuestion.objects.all() if include_deleted else PollQuestion.objects.filter(is_deleted=False)
    return (
        qs.select_related("current_session__created_by", "current_session__closed_by")
        .prefetch_related("current_session__session_options")
        .annotate(session_count=Count("sessions", distinct=True))
    )


def admin_questions_data(questions, now=None):
    """Admin representation: legacy keys (options/total_votes = current session) + schedule and session info."""
    now = now or timezone.now()
    questions = list(questions)
    session_ids = [q.current_session_id for q in questions if q.current_session_id]
    counts = vote_counts(session_ids)
    voters = voter_counts(session_ids)
    live_by_question = {}
    for opt in PollOption.objects.filter(question__in=questions, is_deleted=False):
        live_by_question.setdefault(opt.question_id, []).append(opt)

    result = []
    for q in questions:
        session = q.current_session
        current = session_to_data(session, counts, voters, now) if session else None
        session_counts = counts.get(q.current_session_id, {})
        order = {so.option_id: so.order for so in session.session_options.all()} if session else {}
        live = sorted(live_by_question.get(q.pk, []), key=lambda o: (order.get(o.id, len(order)), str(o.id)))
        total = sum(session_counts.get(o.id, 0) for o in live)
        status_value = session.effective_status(now) if session else CLOSED
        result.append({
            "id": str(q.pk),
            "question_text": q.question_text,
            "question_type": q.question_type,
            "order": q.order,
            "is_active": status_value == ACTIVE,
            "options": [
                {
                    "id": str(o.id),
                    "option_text": o.option_text,
                    "vote_count": session_counts.get(o.id, 0),
                    "vote_percentage": _pct(session_counts.get(o.id, 0), total),
                }
                for o in live
            ],
            "total_votes": total,
            "status": status_value,
            "start_at": _iso(session.start_at) if session else None,
            "end_at": _iso(session.end_at) if session else None,
            "duration_hours": q.duration_hours,
            "created_at": _iso(q.created_at),
            "updated_at": _iso(q.updated_at),
            "session_count": getattr(q, "session_count", None),
            "current_session": current,
        })
    return result


def admin_question_data(question_id):
    question = admin_question_queryset().filter(pk=question_id).first()
    if question is None:
        raise PollError("Poll not found.", 404)
    return admin_questions_data([question])[0]


def active_polls_for_user(user, now=None):
    """
    Data for GET /api/dashboard/polls/active/ — same shape as before sessions existed.
    Only polls whose current session is effectively active; counts and is_voted are current-session only.
    """
    now = now or timezone.now()
    questions = list(
        PollQuestion.objects.filter(is_deleted=False)
        .filter(question_status_q(ACTIVE, now))
        .select_related("current_session")
        .prefetch_related("current_session__session_options__option")
        .order_by("order")
    )
    session_ids = [q.current_session_id for q in questions]
    counts = vote_counts(session_ids)
    voted_sessions = set(
        PollResponse.objects.filter(user=user, session_id__in=session_ids)
        .values_list("session_id", flat=True)
    ) if session_ids else set()

    data = []
    for q in questions:
        session_counts = counts.get(q.current_session_id, {})
        snapshot = [
            so for so in sorted(q.current_session.session_options.all(), key=lambda so: so.order)
            if not so.option.is_deleted
        ]
        total = sum(session_counts.get(so.option_id, 0) for so in snapshot)
        data.append({
            "id": str(q.pk),
            "question_text": q.question_text,
            "question_type": q.question_type,
            "order": q.order,
            "options": [
                {
                    "id": str(so.option_id),
                    "option_text": so.option_text,
                    "vote_count": session_counts.get(so.option_id, 0),
                    "vote_percentage": _pct(session_counts.get(so.option_id, 0), total),
                }
                for so in snapshot
            ],
            "is_voted": q.current_session_id in voted_sessions,
        })
    return data


def poll_stats(now=None):
    now = now or timezone.now()
    polls = PollQuestion.objects.filter(is_deleted=False)
    total = polls.count()
    active = polls.filter(question_status_q(ACTIVE, now)).count()
    scheduled = polls.filter(question_status_q(SCHEDULED, now)).count()
    cancelled = polls.filter(question_status_q(CANCELLED, now)).count()
    current_ids = polls.exclude(current_session__isnull=True).values("current_session_id")
    return {
        "total_polls": total,
        # Legacy meaning: every poll that is not accepting votes.
        "active_polls": active,
        "closed_polls_count": total - active,
        "total_votes": PollResponse.objects.count(),
        "scheduled_polls": scheduled,
        "cancelled_polls": cancelled,
        "ended_polls": total - active - scheduled - cancelled,
        "total_sessions": PollSession.objects.filter(question__is_deleted=False).count(),
        "current_session_votes": PollResponse.objects.filter(session_id__in=current_ids).count(),
    }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _chunks(ids):
    for i in range(0, len(ids), _ID_CHUNK):
        yield ids[i:i + _ID_CHUNK]


def sync_poll_statuses(now=None):
    """
    Move sessions scheduled->active and scheduled/active->closed according to their schedule,
    then mirror PollQuestion.is_active. Only ever moves forward, so closed/cancelled sessions are
    never reopened, and running it repeatedly is a no-op. Uses set-based UPDATEs guarded by the
    expected current status, so a concurrent reset/close wins and is not overwritten.
    """
    now = now or timezone.now()
    to_close = list(
        PollSession.objects.filter(status__in=OPEN_STATUSES, end_at__lte=now)
        .values_list("pk", "question_id", "session_number")
    )
    closed = 0
    for chunk in _chunks([row[0] for row in to_close]):
        closed += PollSession.objects.filter(pk__in=chunk, status__in=OPEN_STATUSES, end_at__lte=now).update(
            status=CLOSED, closed_at=F("end_at"), closed_reason=PollSession.CLOSED_SCHEDULE,
        )

    to_activate = list(
        PollSession.objects.filter(status=SCHEDULED, start_at__lte=now)
        .filter(_not_ended_q("", now))
        .values_list("pk", "question_id", "session_number")
    )
    activated = 0
    for chunk in _chunks([row[0] for row in to_activate]):
        activated += PollSession.objects.filter(pk__in=chunk, status=SCHEDULED, start_at__lte=now).update(
            status=ACTIVE, activated_at=now,
        )

    deactivated = PollQuestion.objects.filter(is_active=True).filter(
        ~Q(current_session__status=ACTIVE) | Q(is_deleted=True) | Q(current_session__isnull=True)
    ).update(is_active=False)
    reactivated = PollQuestion.objects.filter(
        is_active=False, is_deleted=False, current_session__status=ACTIVE,
    ).update(is_active=True)

    for pk, question_id, number in to_close:
        logger.info("poll_auto_close poll=%s session=%s number=%s", question_id, pk, number)
    for pk, question_id, number in to_activate:
        logger.info("poll_auto_activate poll=%s session=%s number=%s", question_id, pk, number)
    return {
        "activated": activated,
        "closed": closed,
        "is_active_synced": deactivated + reactivated,
    }
