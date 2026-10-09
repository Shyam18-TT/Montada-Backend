from contextlib import contextmanager
from io import StringIO
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from Mainapp.models import User

from . import polls
from .models import PollOption, PollQuestion, PollResponse, PollSession

ADMIN = "/api/admin/polls/"
ACTIVE_LIST = "/api/dashboard/polls/active/"
VOTE = "/api/dashboard/polls/vote/"


@contextmanager
def frozen(at):
    with patch("django.utils.timezone.now", return_value=at):
        yield


class PollTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com", username="admin@example.com", password="Testpass123!", is_staff=True,
        )
        self.users = [
            User.objects.create_user(email=f"u{i}@example.com", username=f"u{i}@example.com", password="Testpass123!")
            for i in range(3)
        ]
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def create_poll(self, **extra):
        payload = {"question_text": "Where is EURUSD heading?", "options": ["Up", "Down"], **extra}
        res = self.admin_client.post(f"{ADMIN}create/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["question"]

    def vote(self, user, question, option_index=0, option_ids=None):
        ids = option_ids or [question["options"][option_index]["id"]]
        return self.client_for(user).post(VOTE, {"poll_id": "x", "question_id": question["id"], "option_ids": ids}, format="json")


class PollSchedulingTests(PollTestBase):
    def test_create_defaults_to_seven_day_active_session(self):
        now = timezone.now()
        with frozen(now):
            poll = self.create_poll()
        session = PollSession.objects.get(pk=poll["current_session"]["id"])
        self.assertEqual(session.status, "active")
        self.assertEqual(session.start_at, now)
        self.assertEqual(session.end_at, now + timedelta(days=7))
        self.assertEqual(session.session_number, 1)
        self.assertEqual(session.created_by, self.admin)
        self.assertTrue(poll["is_active"])
        self.assertEqual(poll["status"], "active")
        self.assertEqual(poll["duration_hours"], 168)

    def test_create_with_custom_schedule_is_scheduled(self):
        start = timezone.now() + timedelta(days=1)
        end = start + timedelta(hours=6)
        poll = self.create_poll(start_at=start.isoformat(), end_at=end.isoformat())
        self.assertEqual(poll["status"], "scheduled")
        self.assertFalse(poll["is_active"])
        session = PollSession.objects.get(pk=poll["current_session"]["id"])
        self.assertEqual((session.start_at, session.end_at), (start, end))

    def test_create_with_custom_duration(self):
        poll = self.create_poll(duration_days=3)
        session = PollSession.objects.get(pk=poll["current_session"]["id"])
        self.assertEqual(session.end_at - session.start_at, timedelta(days=3))

    def test_invalid_schedules_rejected(self):
        now = timezone.now()
        cases = [
            {"start_at": (now + timedelta(days=2)).isoformat(), "end_at": (now + timedelta(days=1)).isoformat()},
            {"start_at": (now - timedelta(days=1)).isoformat()},
            {"end_at": (now - timedelta(minutes=1)).isoformat()},
            {"start_at": "not-a-date"},
            {"duration_days": 0},
            {"duration_hours": "abc"},
        ]
        for extra in cases:
            res = self.admin_client.post(f"{ADMIN}create/", {"question_text": "Q", "options": ["a", "b"], **extra}, format="json")
            self.assertEqual(res.status_code, 400, extra)
            self.assertIn("error", res.data)
        self.assertFalse(PollQuestion.objects.exists())

    def test_naive_datetime_uses_project_timezone(self):
        start = (timezone.now() + timedelta(days=1)).replace(microsecond=0, tzinfo=None)
        poll = self.create_poll(start_at=start.isoformat())
        session = PollSession.objects.get(pk=poll["current_session"]["id"])
        self.assertEqual(session.start_at, timezone.make_aware(start, timezone.get_current_timezone()))

    def test_requires_valid_options(self):
        for options in ([], ["only one"], ["a", "b", "c", "d", "e"], "notalist"):
            res = self.admin_client.post(f"{ADMIN}create/", {"question_text": "Q", "options": options}, format="json")
            self.assertEqual(res.status_code, 400)


class PollSchedulerTests(PollTestBase):
    def test_scheduler_activates_then_closes_and_is_idempotent(self):
        start = timezone.now() + timedelta(hours=1)
        poll = self.create_poll(start_at=start.isoformat(), duration_hours=2)
        session_id = poll["current_session"]["id"]

        with frozen(start - timedelta(seconds=1)):
            self.assertEqual(polls.sync_poll_statuses()["activated"], 0)
        with frozen(start):
            self.assertEqual(polls.sync_poll_statuses()["activated"], 1)
            self.assertEqual(polls.sync_poll_statuses(), {"activated": 0, "closed": 0, "is_active_synced": 0})
        session = PollSession.objects.get(pk=session_id)
        self.assertEqual(session.status, "active")
        self.assertTrue(PollQuestion.objects.get(pk=poll["id"]).is_active)

        end = start + timedelta(hours=2)
        with frozen(end):
            result = polls.sync_poll_statuses()
            self.assertEqual(result["closed"], 1)
            self.assertEqual(polls.sync_poll_statuses(), {"activated": 0, "closed": 0, "is_active_synced": 0})
        session.refresh_from_db()
        self.assertEqual((session.status, session.closed_at, session.closed_reason), ("closed", end, "schedule"))
        self.assertFalse(PollQuestion.objects.get(pk=poll["id"]).is_active)
        self.assertEqual(PollSession.objects.count(), 1)

    def test_scheduler_never_reopens_terminal_sessions(self):
        poll = self.create_poll()
        self.admin_client.post(f"{ADMIN}{poll['id']}/cancel/")
        other = self.create_poll()
        self.admin_client.post(f"{ADMIN}{other['id']}/close/")
        call_command("update_poll_statuses", stdout=StringIO())
        statuses = set(PollSession.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {"cancelled", "closed"})

    def test_scheduler_skips_already_expired_scheduled_session(self):
        start = timezone.now() + timedelta(hours=1)
        poll = self.create_poll(start_at=start.isoformat(), duration_hours=1)
        with frozen(start + timedelta(hours=5)):  # scheduler was down for the whole window
            result = polls.sync_poll_statuses()
        self.assertEqual((result["activated"], result["closed"]), (0, 1))
        self.assertEqual(PollSession.objects.get(pk=poll["current_session"]["id"]).status, "closed")

    def test_management_command_runs_once(self):
        call_command("update_poll_statuses", stdout=StringIO())


class PollVotingTests(PollTestBase):
    def test_vote_rejected_before_start_and_after_end_even_without_scheduler(self):
        start = timezone.now() + timedelta(hours=1)
        poll = self.create_poll(start_at=start.isoformat(), duration_hours=1)
        res = self.vote(self.users[0], poll)
        self.assertEqual((res.status_code, res.data["error"]), (400, "This poll has not started yet."))
        with frozen(start):
            self.assertEqual(self.vote(self.users[0], poll).status_code, 201)
        with frozen(start + timedelta(hours=1)):  # exactly end_at: closed
            res = self.vote(self.users[1], poll)
        self.assertEqual((res.status_code, res.data["error"]), (400, "This poll is closed."))
        self.assertEqual(PollSession.objects.get().status, "scheduled")  # scheduler never ran

    def test_vote_success_and_duplicate_prevention(self):
        poll = self.create_poll()
        res = self.vote(self.users[0], poll)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data, {"message": "Vote recorded successfully."})
        res = self.vote(self.users[0], poll, option_index=1)
        self.assertEqual((res.status_code, res.data["error"]), (400, "You have already voted for this question."))
        response = PollResponse.objects.get()
        self.assertEqual(str(response.session_id), poll["current_session"]["id"])

    def test_vote_validation_messages_unchanged(self):
        poll = self.create_poll()
        client = self.client_for(self.users[0])
        cases = [
            ({"option_ids": ["x"]}, 400, "question_id is required."),
            ({"question_id": poll["id"], "option_ids": []}, 400, "option_ids must be a non-empty list."),
            ({"question_id": "bad", "option_ids": ["x"]}, 400, "Invalid question_id."),
            ({"question_id": "00000000-0000-0000-0000-000000000000", "option_ids": ["x"]}, 404, "Question not found."),
            ({"question_id": poll["id"], "option_ids": [o["id"] for o in poll["options"]]}, 400,
             "This question allows only one option (single choice)."),
            ({"question_id": poll["id"], "option_ids": ["bad"]}, 400, "Invalid option_id: bad."),
            ({"question_id": poll["id"], "option_ids": ["00000000-0000-0000-0000-000000000000"]}, 400,
             "One or more option_ids are not valid for this question."),
        ]
        for body, code, message in cases:
            res = client.post(VOTE, body, format="json")
            self.assertEqual((res.status_code, res.data["error"]), (code, message), body)

    def test_multiple_choice_vote(self):
        poll = self.create_poll(question_type="multiple", options=["a", "b", "c"])
        ids = [o["id"] for o in poll["options"][:2]]
        self.assertEqual(self.vote(self.users[0], poll, option_ids=ids + ids[:1]).status_code, 201)
        self.assertEqual(PollResponse.objects.count(), 2)

    def test_integrity_error_reported_as_duplicate(self):
        poll = self.create_poll()
        with patch.object(PollResponse.objects, "bulk_create", side_effect=IntegrityError):
            res = self.vote(self.users[0], poll)
        self.assertEqual((res.status_code, res.data["error"]), (400, "You have already voted for this question."))

    def test_db_constraint_blocks_duplicate_row(self):
        poll = self.create_poll()
        self.vote(self.users[0], poll)
        existing = PollResponse.objects.get()
        with self.assertRaises(IntegrityError):
            PollResponse.objects.create(
                question_id=existing.question_id, session_id=existing.session_id,
                option_id=existing.option_id, user=existing.user,
            )

    def test_active_list_response_shape_unchanged(self):
        poll = self.create_poll()
        self.create_poll(question_text="future", start_at=(timezone.now() + timedelta(days=1)).isoformat())
        self.vote(self.users[0], poll)
        res = self.client_for(self.users[0]).get(ACTIVE_LIST)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(list(res.data), ["polls"])
        self.assertEqual(len(res.data["polls"]), 1)
        (question,) = res.data["polls"][0]["questions"]
        self.assertEqual(set(question), {"id", "question_text", "question_type", "order", "options", "is_voted"})
        self.assertEqual(set(question["options"][0]), {"id", "option_text", "vote_count", "vote_percentage"})
        self.assertTrue(question["is_voted"])
        self.assertEqual([o["vote_count"] for o in question["options"]], [1, 0])
        self.assertEqual([o["vote_percentage"] for o in question["options"]], [100.0, 0])
        self.assertEqual(self.client_for(self.users[1]).get(ACTIVE_LIST).data["polls"][0]["questions"][0]["is_voted"], False)

    def test_active_list_empty(self):
        self.assertEqual(self.client_for(self.users[0]).get(ACTIVE_LIST).data, {"polls": []})


class PollResetTests(PollTestBase):
    def test_reset_preserves_history_and_allows_revote(self):
        poll = self.create_poll()
        self.vote(self.users[0], poll, 0)
        self.vote(self.users[1], poll, 0)
        self.vote(self.users[2], poll, 1)
        first_session = poll["current_session"]["id"]

        res = self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {"duration_days": 2}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        new = res.data["question"]
        self.assertEqual(new["id"], poll["id"])
        self.assertEqual(new["current_session"]["session_number"], 2)
        self.assertEqual(new["current_session"]["created_reason"], "reset")
        self.assertEqual(new["current_session"]["created_by"]["email"], "admin@example.com")
        self.assertEqual(new["total_votes"], 0)

        old = PollSession.objects.get(pk=first_session)
        self.assertEqual((old.status, old.closed_reason, old.closed_by), ("closed", "reset", self.admin))
        self.assertEqual(PollResponse.objects.filter(session=old).count(), 3)

        # User list shows the fresh session; users may vote again.
        listed = self.client_for(self.users[0]).get(ACTIVE_LIST).data["polls"][0]["questions"][0]
        self.assertFalse(listed["is_voted"])
        self.assertEqual([o["vote_count"] for o in listed["options"]], [0, 0])
        self.assertEqual(self.vote(self.users[0], poll, 1).status_code, 201)
        self.assertEqual(self.vote(self.users[0], poll, 1).status_code, 400)

        # History is unchanged by the new session.
        res = self.admin_client.get(f"{ADMIN}{poll['id']}/sessions/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 2)
        self.assertEqual(res.data["poll"]["session_count"], 2)
        latest, first = res.data["results"]
        self.assertEqual((latest["session_number"], latest["total_votes"]), (2, 1))
        self.assertEqual((first["session_number"], first["total_votes"], first["total_voters"]), (1, 3, 3))
        self.assertEqual([o["vote_count"] for o in first["options"]], [2, 1])
        self.assertEqual([o["vote_percentage"] for o in first["options"]], [66.67, 33.33])

        detail = self.admin_client.get(f"{ADMIN}{poll['id']}/sessions/{first_session}/")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.data["is_current"])
        self.assertEqual(detail.data["session"]["total_votes"], 3)

        current = self.admin_client.get(f"{ADMIN}{poll['id']}/results/")
        self.assertEqual(current.data["session"]["session_number"], 2)
        self.assertEqual([o["vote_count"] for o in current.data["session"]["options"]], [0, 1])

    def test_historical_options_survive_option_changes(self):
        poll = self.create_poll()
        self.vote(self.users[0], poll, 0)
        first_session = poll["current_session"]["id"]
        self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {}, format="json")
        up, down = poll["options"]
        res = self.admin_client.patch(
            f"{ADMIN}{poll['id']}/",
            {"options": [{"id": down["id"], "option_text": "Lower"}, {"option_text": "Sideways"}]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([o["option_text"] for o in res.data["question"]["options"]], ["Lower", "Sideways"])
        self.assertTrue(PollOption.objects.get(pk=up["id"]).is_deleted)

        old = self.admin_client.get(f"{ADMIN}{poll['id']}/sessions/{first_session}/").data["session"]
        self.assertEqual([(o["option_text"], o["vote_count"]) for o in old["options"]], [("Up", 1), ("Down", 0)])
        listed = self.client_for(self.users[1]).get(ACTIVE_LIST).data["polls"][0]["questions"][0]
        self.assertEqual([o["option_text"] for o in listed["options"]], ["Lower", "Sideways"])

    def test_options_with_current_votes_cannot_be_changed(self):
        poll = self.create_poll(options=["a", "b", "c"])
        self.vote(self.users[0], poll, 0)
        voted = poll["options"][0]
        res = self.admin_client.patch(
            f"{ADMIN}{poll['id']}/", {"options": [{"id": voted["id"], "option_text": "renamed"}, *poll["options"][1:]]},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        res = self.admin_client.delete(f"{ADMIN}{poll['id']}/options/{voted['id']}/")
        self.assertEqual(res.status_code, 400)
        res = self.admin_client.patch(f"{ADMIN}{poll['id']}/", {"question_type": "multiple"}, format="json")
        self.assertEqual(res.status_code, 400)
        # An unvoted option can still be removed, and new ones added.
        self.assertEqual(self.admin_client.delete(f"{ADMIN}{poll['id']}/options/{poll['options'][2]['id']}/").status_code, 200)
        self.assertEqual(self.admin_client.post(f"{ADMIN}{poll['id']}/options/", {"option_text": "d"}, format="json").status_code, 201)
        self.assertEqual(PollOption.objects.filter(question_id=poll["id"], is_deleted=False).count(), 3)

    def test_reset_rolls_back_on_failure(self):
        poll = self.create_poll()
        self.vote(self.users[0], poll)
        with patch.object(polls, "_snapshot_options", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {}, format="json")
        question = PollQuestion.objects.get(pk=poll["id"])
        self.assertEqual(str(question.current_session_id), poll["current_session"]["id"])
        self.assertEqual(PollSession.objects.count(), 1)
        self.assertEqual(PollSession.objects.get().status, "active")
        self.assertEqual(PollResponse.objects.count(), 1)

    def test_stale_reset_is_rejected(self):
        poll = self.create_poll()
        stale = poll["current_session"]["id"]
        first = self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {"current_session_id": stale}, format="json")
        second = self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {"current_session_id": stale}, format="json")
        self.assertEqual((first.status_code, second.status_code), (201, 409))
        self.assertEqual(PollSession.objects.count(), 2)

    def test_vote_in_session_closed_by_concurrent_reset_is_rejected(self):
        poll = self.create_poll()
        question = PollQuestion.objects.get(pk=poll["id"])
        old_session = question.current_session
        polls.reset_poll(self.admin, question.pk, {})
        # Simulate a vote request that read the poll before the reset committed.
        stale = PollQuestion.objects.get(pk=question.pk)
        stale.current_session_id = old_session.pk
        with patch.object(PollQuestion.objects, "get", return_value=stale):
            with self.assertRaises(polls.PollError) as ctx:
                polls.cast_vote(self.users[0], str(question.pk), [poll["options"][0]["id"]])
        self.assertEqual(ctx.exception.message, "This poll is closed.")
        self.assertFalse(PollResponse.objects.exists())

    def test_reset_of_scheduled_session_cancels_it(self):
        poll = self.create_poll(start_at=(timezone.now() + timedelta(days=1)).isoformat())
        self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {}, format="json")
        self.assertEqual(PollSession.objects.get(pk=poll["current_session"]["id"]).status, "cancelled")


class PollAdminLifecycleTests(PollTestBase):
    def test_close_reopen_unpublish_activate_cancel(self):
        poll = self.create_poll()
        res = self.admin_client.post(f"{ADMIN}{poll['id']}/close/")
        self.assertEqual((res.data["message"], res.data["is_active"], res.data["status"]), ("Poll closed.", False, "closed"))
        self.assertEqual(self.vote(self.users[0], poll).status_code, 400)
        res = self.admin_client.post(f"{ADMIN}{poll['id']}/close/", {"reopen": True}, format="json")
        self.assertEqual((res.data["message"], res.data["is_active"]), ("Poll reopened.", True))
        self.assertEqual(self.vote(self.users[0], poll).status_code, 201)

        res = self.admin_client.post(f"{ADMIN}{poll['id']}/unpublish/")
        self.assertEqual(res.data, {"message": "Poll unpublished.", "id": poll["id"], "is_active": False})
        self.assertEqual(self.admin_client.post(f"{ADMIN}{poll['id']}/activate/").status_code, 200)

        self.assertEqual(self.admin_client.post(f"{ADMIN}{poll['id']}/cancel/").status_code, 200)
        res = self.admin_client.post(f"{ADMIN}{poll['id']}/activate/")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(PollResponse.objects.count(), 1)

    def test_activate_scheduled_poll_starts_now(self):
        poll = self.create_poll(start_at=(timezone.now() + timedelta(days=1)).isoformat())
        res = self.admin_client.post(f"{ADMIN}{poll['id']}/activate/")
        self.assertEqual(res.data["question"]["status"], "active")
        self.assertEqual(self.vote(self.users[0], poll).status_code, 201)

    def test_reopen_after_end_requires_new_end(self):
        start = timezone.now()
        poll = self.create_poll(duration_hours=1)
        with frozen(start + timedelta(hours=2)):
            self.assertEqual(self.admin_client.post(f"{ADMIN}{poll['id']}/activate/").status_code, 400)
            end = (start + timedelta(hours=5)).isoformat()
            res = self.admin_client.post(f"{ADMIN}{poll['id']}/activate/", {"end_at": end}, format="json")
            self.assertEqual(res.status_code, 200, res.data)

    def test_update_schedule_rules(self):
        start = timezone.now() + timedelta(days=1)
        poll = self.create_poll(start_at=start.isoformat())
        new_end = (start + timedelta(days=3)).isoformat()
        res = self.admin_client.patch(f"{ADMIN}{poll['id']}/", {"end_at": new_end}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["question"]["end_at"], (start + timedelta(days=3)).isoformat())
        res = self.admin_client.patch(f"{ADMIN}{poll['id']}/", {"end_at": start.isoformat()}, format="json")
        self.assertEqual(res.status_code, 400)

        active = self.create_poll()
        res = self.admin_client.patch(
            f"{ADMIN}{active['id']}/", {"start_at": (timezone.now() + timedelta(days=1)).isoformat()}, format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_delete_keeps_voted_history(self):
        unused = self.create_poll()
        self.assertEqual(self.admin_client.delete(f"{ADMIN}{unused['id']}/").status_code, 200)
        self.assertFalse(PollQuestion.objects.filter(pk=unused["id"]).exists())

        voted = self.create_poll()
        self.vote(self.users[0], voted)
        self.assertEqual(self.admin_client.delete(f"{ADMIN}{voted['id']}/").status_code, 200)
        question = PollQuestion.objects.get(pk=voted["id"])
        self.assertTrue(question.is_deleted)
        self.assertEqual(PollResponse.objects.count(), 1)
        self.assertEqual(self.admin_client.get(f"{ADMIN}{voted['id']}/").status_code, 404)
        self.assertEqual(self.admin_client.get(f"{ADMIN}{voted['id']}/sessions/").data["count"], 1)
        self.assertEqual(self.client_for(self.users[1]).get(ACTIVE_LIST).data, {"polls": []})

    def test_list_filters_pagination_and_stats(self):
        self.create_poll(question_text="live")
        self.create_poll(question_text="future", start_at=(timezone.now() + timedelta(days=2)).isoformat())
        closed = self.create_poll(question_text="done")
        self.admin_client.post(f"{ADMIN}{closed['id']}/close/")

        def texts(**params):
            res = self.admin_client.get(ADMIN, params)
            self.assertEqual(res.status_code, 200)
            return sorted(r["question_text"] for r in res.data["results"])

        self.assertEqual(texts(), ["done", "future", "live"])
        self.assertEqual(texts(status="active"), ["live"])
        self.assertEqual(texts(status="scheduled"), ["future"])
        self.assertEqual(texts(status="closed"), ["done"])
        self.assertEqual(texts(status="unpublished"), ["done", "future"])
        tomorrow = (timezone.now() + timedelta(days=1)).date().isoformat()
        self.assertEqual(texts(start_from=tomorrow), ["future"])
        self.assertEqual(self.admin_client.get(ADMIN, {"start_from": "garbage"}).status_code, 400)
        res = self.admin_client.get(ADMIN, {"page_size": 2})
        self.assertEqual((res.data["count"], len(res.data["results"])), (3, 2))

        stats = self.admin_client.get(f"{ADMIN}stats/").data
        self.assertEqual(
            (stats["total_polls"], stats["active_polls"], stats["closed_polls_count"], stats["scheduled_polls"]),
            (3, 1, 2, 1),
        )


class PollPermissionTests(PollTestBase):
    def test_admin_endpoints_require_staff(self):
        poll = self.create_poll()
        pid, sid = poll["id"], poll["current_session"]["id"]
        oid = poll["options"][0]["id"]
        endpoints = [
            ("get", f"{ADMIN}stats/"), ("get", ADMIN), ("post", f"{ADMIN}create/"),
            ("get", f"{ADMIN}{pid}/"), ("patch", f"{ADMIN}{pid}/"), ("delete", f"{ADMIN}{pid}/"),
            ("post", f"{ADMIN}{pid}/unpublish/"), ("post", f"{ADMIN}{pid}/close/"),
            ("post", f"{ADMIN}{pid}/activate/"), ("post", f"{ADMIN}{pid}/cancel/"), ("post", f"{ADMIN}{pid}/reset/"),
            ("get", f"{ADMIN}{pid}/results/"), ("get", f"{ADMIN}{pid}/sessions/"),
            ("get", f"{ADMIN}{pid}/sessions/{sid}/"), ("post", f"{ADMIN}{pid}/options/"),
            ("delete", f"{ADMIN}{pid}/options/{oid}/"),
        ]
        user_client, anon = self.client_for(self.users[0]), APIClient()
        for method, url in endpoints:
            self.assertEqual(getattr(user_client, method)(url, {}, format="json").status_code, 403, url)
            self.assertIn(getattr(anon, method)(url, {}, format="json").status_code, (401, 403), url)
        self.assertEqual(PollSession.objects.count(), 1)
        self.assertEqual(APIClient().get(ACTIVE_LIST).status_code, 401)

    def test_unknown_ids_return_404(self):
        poll = self.create_poll()
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.admin_client.post(f"{ADMIN}{missing}/reset/").status_code, 404)
        self.assertEqual(self.admin_client.get(f"{ADMIN}{missing}/sessions/").status_code, 404)
        self.assertEqual(self.admin_client.get(f"{ADMIN}{poll['id']}/sessions/{missing}/").status_code, 404)
        res = self.admin_client.post(f"{ADMIN}{poll['id']}/reset/", {"current_session_id": "nope"}, format="json")
        self.assertEqual(res.status_code, 400)
