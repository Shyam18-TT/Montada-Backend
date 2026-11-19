"""Helpers for the "was this closed trade idea favourable?" vote."""
from collections import defaultdict

from django.db.models import Count, Q

from .models import AppliedSignal, SignalFeedback, TradingSignal

EMPTY_COUNTS = {"favorable": 0, "unfavorable": 0}


def _pct(numerator, denominator):
    """Percentage rounded to 2 places; None (not 0) when there is nothing to divide by."""
    if not denominator:
        return None
    return round(numerator / denominator * 100, 2)


def outcome_of(is_win, is_loss):
    """How a closed signal ended: 'profit' | 'loss' | 'neutral' (same rule as the notifications)."""
    if is_win:
        return "profit"
    if is_loss:
        return "loss"
    return "neutral"


def vote_breakdown(outcome, favorable, unfavorable, my_vote=None):
    """
    Vote tallies for one closed signal, seen against how it ended.

    A vote *agrees with the result* when it is favourable on a profit or unfavourable on a loss.
    Neutral closes have no right answer, so alignment fields are None for them.
    ``my_vote`` is True/False/None (the viewer's is_favorable).
    """
    total = favorable + unfavorable
    if favorable > unfavorable:
        verdict = "favorable"
    elif unfavorable > favorable:
        verdict = "unfavorable"
    else:
        verdict = "split" if total else None
    aligned = contrary = None
    if outcome in ("profit", "loss"):
        aligned = favorable if outcome == "profit" else unfavorable
        contrary = total - aligned
    return {
        "favorable_count": favorable,
        "unfavorable_count": unfavorable,
        "total_votes": total,
        "favorable_pct": _pct(favorable, total),
        "unfavorable_pct": _pct(unfavorable, total),
        "community_verdict": verdict,
        "close_outcome": outcome,
        "aligned_votes": aligned,
        "contrary_votes": contrary,
        "alignment_pct": _pct(aligned, total) if aligned is not None else None,
        "my_vote_matches_outcome": (
            None if my_vote is None or aligned is None else (my_vote is (outcome == "profit"))
        ),
    }


def analyst_feedback_stats(analyst):
    """
    Community feedback on one analyst's closed (non-deleted) trade ideas.

    Vote-level rates use every vote as the denominator; signal-level counts use the majority of
    each signal's votes (ties are 'tied'). ``by_outcome`` repeats the numbers for profit / loss /
    neutral closes, and ``alignment_rate`` is the share of votes on profit/loss closes that agree
    with the result (favourable on a profit, unfavourable on a loss). Rates are None when undefined.
    """
    closed = TradingSignal.active.filter(analyst=analyst, status=TradingSignal.Status.CLOSED)
    closed_ids_outcome = {
        pk: outcome_of(w, l) for pk, w, l in closed.values_list("pk", "is_win", "is_loss")
    }
    per_signal = (
        SignalFeedback.objects.filter(signal_id__in=list(closed_ids_outcome))
        .values("signal_id")
        .annotate(
            fav=Count("id", filter=Q(is_favorable=True)),
            unfav=Count("id", filter=Q(is_favorable=False)),
        )
    )

    def bucket():
        return {"closed_signals": 0, "polled_signals": 0, "favorable_votes": 0, "unfavorable_votes": 0}

    by_outcome = {k: bucket() for k in ("profit", "loss", "neutral")}
    for outcome in closed_ids_outcome.values():
        by_outcome[outcome]["closed_signals"] += 1
    fav_signals = unfav_signals = tied_signals = 0
    aligned_votes = decided_votes = 0
    for row in per_signal:
        fav, unfav = row["fav"], row["unfav"]
        if fav + unfav == 0:
            continue
        outcome = closed_ids_outcome[row["signal_id"]]
        b = by_outcome[outcome]
        b["polled_signals"] += 1
        b["favorable_votes"] += fav
        b["unfavorable_votes"] += unfav
        if fav > unfav:
            fav_signals += 1
        elif unfav > fav:
            unfav_signals += 1
        else:
            tied_signals += 1
        if outcome in ("profit", "loss"):
            decided_votes += fav + unfav
            aligned_votes += fav if outcome == "profit" else unfav

    for b in by_outcome.values():
        b["total_votes"] = b["favorable_votes"] + b["unfavorable_votes"]
        b["favorable_rate"] = _pct(b["favorable_votes"], b["total_votes"])
        b["unfavorable_rate"] = _pct(b["unfavorable_votes"], b["total_votes"])

    closed_total = len(closed_ids_outcome)
    polled = fav_signals + unfav_signals + tied_signals
    fav_votes = sum(b["favorable_votes"] for b in by_outcome.values())
    unfav_votes = sum(b["unfavorable_votes"] for b in by_outcome.values())
    total_votes = fav_votes + unfav_votes
    return {
        "closed_signals": closed_total,
        "polled_signals": polled,
        "unpolled_signals": closed_total - polled,
        "poll_participation_rate": _pct(polled, closed_total),
        "total_votes": total_votes,
        "favorable_votes": fav_votes,
        "unfavorable_votes": unfav_votes,
        "favorable_vote_rate": _pct(fav_votes, total_votes),
        "unfavorable_vote_rate": _pct(unfav_votes, total_votes),
        "favorable_signals": fav_signals,
        "unfavorable_signals": unfav_signals,
        "tied_signals": tied_signals,
        "favorable_signal_rate": _pct(fav_signals, polled),
        "alignment_rate": _pct(aligned_votes, decided_votes),
        "by_outcome": by_outcome,
    }


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
                **vote_breakdown(
                    outcome_of(s.is_win, s.is_loss), c["favorable"], c["unfavorable"],
                    my_vote=my_votes.get(s.pk),
                ),
            }
        result[str(s.pk)] = {"applied": applied, "feedback": feedback}
    return result
