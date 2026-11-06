"""Helpers for the "was this closed trade idea favourable?" vote."""
from collections import defaultdict

from django.db.models import Count

from .models import AppliedSignal, SignalFeedback, TradingSignal

EMPTY_COUNTS = {"favorable": 0, "unfavorable": 0}


def build_feedback_map(user, signals):
    """
    One entry per signal id (str) for the viewer, computed with three queries however many
    signals there are: ``{"applied": bool, "feedback": dict | None}``.
    ``feedback`` is None unless the signal is CLOSED.
    """
    signals = [s for s in signals if s is not None]
    ids = [s.pk for s in signals]
    closed_ids = [s.pk for s in signals if s.status == TradingSignal.Status.CLOSED]
    authed = bool(user and getattr(user, "is_authenticated", False))

    applied_ids = set()
    if authed and ids:
        applied_ids = set(
            AppliedSignal.objects.filter(trader=user, signal_id__in=ids).values_list("signal_id", flat=True)
        )

    counts = defaultdict(lambda: dict(EMPTY_COUNTS))
    my_votes = {}
    if closed_ids:
        for row in (
            SignalFeedback.objects.filter(signal_id__in=closed_ids)
            .values("signal_id", "is_favorable")
            .annotate(n=Count("id"))
        ):
            key = "favorable" if row["is_favorable"] else "unfavorable"
            counts[row["signal_id"]][key] = row["n"]
        if authed:
            my_votes = dict(
                SignalFeedback.objects.filter(signal_id__in=closed_ids, voter=user)
                .values_list("signal_id", "is_favorable")
            )

    result = {}
    for s in signals:
        applied = (s.pk in applied_ids) if authed else None  # None: no viewer, unknown
        feedback = None
        if s.status == TradingSignal.Status.CLOSED:
            c = counts[s.pk] if s.pk in counts else dict(EMPTY_COUNTS)
            voted = s.pk in my_votes
            feedback = {
                "can_vote": bool(applied) and not s.is_deleted,
                "has_voted": voted,
                "my_vote": (("favorable" if my_votes[s.pk] else "unfavorable") if voted else None),
                "favorable_count": c["favorable"],
                "unfavorable_count": c["unfavorable"],
                "total_votes": c["favorable"] + c["unfavorable"],
            }
        result[str(s.pk)] = {"applied": applied, "feedback": feedback}
    return result
