from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from Followers.models import Follow, Mute
from MontadaAdmin.user_status import SUSPENDED, INACTIVE, filter_users_by_status, user_status

User = get_user_model()


def make_user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="Testpass123!", is_verified=True, **extra
    )


class SuspendedAndDeletedUsersHiddenTests(APITestCase):
    def setUp(self):
        self.me = make_user("me@example.com", user_type="trader")
        self.client = APIClient()
        self.client.force_authenticate(self.me)

        self.ok = make_user("ok@example.com", user_type="analyst")
        self.suspended = make_user("sus@example.com", user_type="analyst")
        self.deleted = make_user("del@example.com", user_type="analyst")
        self.suspended.is_active = False
        self.suspended.save(update_fields=["is_active"])
        # Legacy shape: flagged deleted but is_active still True.
        self.deleted.is_soft_deleted = True
        self.deleted.soft_deleted_at = timezone.now()
        self.deleted.save(update_fields=["is_soft_deleted", "soft_deleted_at"])

        for analyst in (self.ok, self.suspended, self.deleted):
            Follow.objects.create(
                follower=self.me, followed=analyst,
                status=Follow.Status.ACCEPTED, is_active=True,
            )
            Mute.objects.create(muter=self.me, muted=analyst)

    def test_status_distinguishes_deleted_and_suspended(self):
        self.assertEqual(user_status(self.suspended), SUSPENDED)
        self.assertEqual(user_status(self.deleted), INACTIVE)
        qs = User.objects.all()
        self.assertEqual(list(filter_users_by_status(qs, "suspended")), [self.suspended])
        self.assertEqual(list(filter_users_by_status(qs, "deleted")), [self.deleted])
        self.assertEqual(list(filter_users_by_status(qs, "inactive")), [self.deleted])

    def _ids(self, items):
        return {str(i["id"]) for i in items}

    def test_following_and_muted_lists_hide_them(self):
        res = self.client.get(reverse("Followers:following_list"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._ids(res.data["following"]), {str(self.ok.id)})

        res = self.client.get(reverse("Followers:muted_list"))
        self.assertEqual(self._ids(res.data["muted"]), {str(self.ok.id)})

    def test_counts_exclude_them(self):
        res = self.client.get(reverse("Followers:counts"))
        self.assertEqual(res.data["following_count"], 1)
        self.assertEqual(res.data["muted_count"], 1)

    def test_analysts_list_and_profile(self):
        res = self.client.get(reverse("Followers:analysts_list"))
        self.assertEqual(res.status_code, 200)
        payload = res.data.get("results", res.data.get("analysts", res.data))
        self.assertEqual(self._ids(payload), {str(self.ok.id)})

        for hidden in (self.suspended, self.deleted):
            res = self.client.get(reverse("Followers:analyst_public_profile", args=[hidden.id]))
            self.assertEqual(res.status_code, 404)

    def test_followers_list_hides_suspended_follower(self):
        Follow.objects.create(
            follower=self.suspended, followed=self.me,
            status=Follow.Status.ACCEPTED, is_active=True,
        )
        Follow.objects.create(
            follower=self.ok, followed=self.me,
            status=Follow.Status.ACCEPTED, is_active=True,
        )
        res = self.client.get(reverse("Followers:followers_list"))
        self.assertEqual(res.data["followers_count"], 1)
        self.assertEqual(res.data["count"], 1)
