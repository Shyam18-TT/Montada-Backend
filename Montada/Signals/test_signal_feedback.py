from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from Followers.models import Follow
from Signals.models import (
    AppliedSignal,
    AssetClass,
    Instrument,
    SignalFeedback,
    Timeframe,
    TradingSignal,
)
from Signals.views import _notify_signal_closed, notify_applied_traders_to_vote

User = get_user_model()


def _user(name, user_type="trader"):
    return User.objects.create_user(
        username=f"{name}@example.com", email=f"{name}@example.com", password="x", user_type=user_type,
    )


class _FeedbackBase(TestCase):
    def setUp(self):
        self.analyst = _user("analyst", "analyst")
        self.t1 = _user("t1")
        self.t2 = _user("t2")
        self.outsider = _user("outsider")
        ac = AssetClass.objects.create(name="Forex")
        self.instrument = Instrument.objects.create(asset_class=ac, symbol="EURUSD")
        self.timeframe = Timeframe.objects.create(code="H1", name="1 Hour")
        self.signal = TradingSignal.objects.create(
            analyst=self.analyst, asset_class=ac, instrument=self.instrument, timeframe=self.timeframe,
            direction="BUY", entry_price="1.10000", stop_loss="1.09000", take_profit="1.12000",
            confidence_level=70, status="OPEN",
        )
        AppliedSignal.objects.create(trader=self.t1, signal=self.signal)
        self.url = f"/api/signals/{self.signal.id}/feedback/"
        self.c1 = APIClient()
        self.c1.force_authenticate(self.t1)

    def _close(self, **flags):
        self.signal.status = "CLOSED"
        self.signal.is_win = flags.get("is_win", True)
        self.signal.is_loss = flags.get("is_loss", False)
        self.signal.is_neutral = False
        self.signal.save()


class SignalFeedbackTests(_FeedbackBase):
    # --- voting rules ----------------------------------------------------

    def test_cannot_vote_while_open(self):
        res = self.c1.post(self.url, {"is_favorable": True}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_applied_trader_can_vote_change_and_withdraw(self):
        self._close()
        res = self.c1.post(self.url, {"is_favorable": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["feedback"]["my_vote"], "favorable")
        res = self.c1.put(self.url, {"vote": "unfavorable"}, format="json")
        fb = res.json()["feedback"]
        self.assertEqual((fb["favorable_count"], fb["unfavorable_count"], fb["total_votes"]), (0, 1, 1))
        self.assertEqual(SignalFeedback.objects.filter(signal=self.signal).count(), 1)
        res = self.c1.delete(self.url)
        self.assertFalse(res.json()["feedback"]["has_voted"])
        self.assertEqual(SignalFeedback.objects.count(), 0)

    def test_not_applied_and_analyst_forbidden(self):
        self._close()
        for user in (self.outsider, self.analyst):
            c = APIClient()
            c.force_authenticate(user)
            self.assertEqual(c.post(self.url, {"is_favorable": True}, format="json").status_code, 403)

    def test_invalid_body_and_missing_signal(self):
        self._close()
        for body in ({}, {"is_favorable": "maybe"}, {"is_favorable": 1}, {"vote": "meh"}):
            self.assertEqual(self.c1.post(self.url, body, format="json").status_code, 400, body)
        import uuid
        self.assertEqual(self.c1.post(f"/api/signals/{uuid.uuid4()}/feedback/", {}, format="json").status_code, 404)

    def test_soft_deleted_signal_is_404(self):
        self._close()
        self.signal.soft_delete()
        self.assertEqual(self.c1.post(self.url, {"is_favorable": True}, format="json").status_code, 404)

    def test_reopen_clears_votes_and_reclose_asks_again(self):
        self._close()
        self.c1.post(self.url, {"is_favorable": True}, format="json")
        with patch("Signals.views._send_push_notifications"), patch("Signals.views._create_and_broadcast_notifications"):
            self.assertEqual(len(notify_applied_traders_to_vote(self.signal)), 1)
        self.signal.refresh_from_db()
        self.signal.status = "OPEN"
        self.signal.is_win = self.signal.is_loss = self.signal.is_neutral = None
        self.signal.save()
        self.signal.refresh_from_db()
        self.assertIsNone(self.signal.feedback_requested_at)
        self.assertEqual(SignalFeedback.objects.count(), 0)
        self._close()
        with patch("Signals.views._send_push_notifications"), patch("Signals.views._create_and_broadcast_notifications"):
            self.assertEqual(len(notify_applied_traders_to_vote(self.signal)), 1)

    # --- serializer fields -----------------------------------------------

    def test_list_fields_applied_polled_and_counts(self):
        AppliedSignal.objects.create(trader=self.t2, signal=self.signal)
        self._close()
        self.c1.post(self.url, {"is_favorable": True}, format="json")
        c2 = APIClient()
        c2.force_authenticate(self.t2)
        c2.post(self.url, {"is_favorable": False}, format="json")

        row = self.c1.get(f"/api/signals/analyst/{self.analyst.id}/signals/?status=CLOSED")
        # not subscribed (403) is fine; use the analyst's own list for the count check instead
        ca = APIClient()
        ca.force_authenticate(self.analyst)
        row = ca.get("/api/signals/my-signals/").json()["results"][0]
        self.assertFalse(row["is_applied"])
        self.assertEqual(row["feedback"]["total_votes"], 2)
        self.assertEqual(row["feedback"]["favorable_count"], 1)
        self.assertFalse(row["feedback"]["can_vote"])

        mine = self.c1.get("/api/signals/my-applied-signals/").json()
        sig = (mine["results"] if isinstance(mine, dict) else mine)[0]["signal"]
        self.assertTrue(sig["is_polled"] is None or sig["is_polled"] is True)

    def test_open_signal_has_no_feedback_block(self):
        ca = APIClient()
        ca.force_authenticate(self.analyst)
        row = ca.get("/api/signals/my-signals/").json()["results"][0]
        self.assertIsNone(row["feedback"])
        self.assertFalse(row["is_polled"])

    def test_get_endpoint_state(self):
        self._close()
        data = self.c1.get(self.url).json()
        self.assertTrue(data["is_applied"])
        self.assertTrue(data["feedback"]["can_vote"])
        self.assertFalse(data["feedback"]["has_voted"])

    # --- notifications ---------------------------------------------------

    @patch("Signals.views._send_push_notifications")
    @patch("Signals.views._create_and_broadcast_notifications")
    def test_applied_traders_get_vote_request_once_and_not_the_plain_one(self, mock_inapp, mock_push):
        Follow.objects.create(follower=self.t1, followed=self.analyst, status=Follow.Status.ACCEPTED, is_active=True)
        Follow.objects.create(follower=self.outsider, followed=self.analyst, status=Follow.Status.ACCEPTED, is_active=True)
        self._close()

        _notify_signal_closed(self.signal, old_status="OPEN")
        calls = mock_push.call_args_list
        self.assertEqual(len(calls), 2)
        vote = next(c for c in calls if c.kwargs["data"].get("vote_requested") == "true")
        plain = next(c for c in calls if c.kwargs["data"].get("vote_requested") != "true")
        self.assertEqual([u.id for u in vote.args[0]], [self.t1.id])
        self.assertEqual([u.id for u in plain.args[0]], [self.outsider.id])  # t1 not told twice
        self.assertIn("vote", vote.kwargs["body"].lower())
        self.assertEqual(vote.kwargs["data"]["type"], "signal_closed")

        # Same close again (retry / auto-close racing the analyst call): no second vote request.
        self.assertEqual(notify_applied_traders_to_vote(self.signal), [])

    @patch("Signals.views._send_push_notifications")
    @patch("Signals.views._create_and_broadcast_notifications")
    def test_applied_non_follower_still_notified_and_inactive_skipped(self, mock_inapp, mock_push):
        AppliedSignal.objects.create(trader=self.t2, signal=self.signal)
        self.t2.is_active = False
        self.t2.save()
        self._close()
        users = notify_applied_traders_to_vote(self.signal)
        self.assertEqual([u.id for u in users], [self.t1.id])

    def test_open_signal_never_requests_votes(self):
        self.assertEqual(notify_applied_traders_to_vote(self.signal), [])
        self.signal.refresh_from_db()
        self.assertIsNone(self.signal.feedback_requested_at)


class AnalystFollowerFeedbackTests(_FeedbackBase):
    """Analysts who follow another analyst and applied their idea vote and are notified like traders."""

    def setUp(self):
        super().setUp()
        self.peer = _user("peer", "analyst")
        Follow.objects.create(follower=self.peer, followed=self.analyst, status=Follow.Status.ACCEPTED, is_active=True)
        AppliedSignal.objects.create(trader=self.peer, signal=self.signal)

    @patch("Signals.views._send_push_notifications")
    @patch("Signals.views._create_and_broadcast_notifications")
    def test_peer_analyst_notified_and_can_vote(self, mock_inapp, mock_push):
        self._close()
        users = notify_applied_traders_to_vote(self.signal)
        self.assertIn(self.peer.id, {u.id for u in users})
        c = APIClient()
        c.force_authenticate(self.peer)
        res = c.post(self.url, {"is_favorable": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["feedback"]["my_vote"], "favorable")
        row = c.get("/api/signals/following-analysts-signals/?status=CLOSED").json()["results"][0]
        self.assertTrue(row["is_applied"])
        self.assertTrue(row["is_polled"])
