from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from Mainapp.models import User, UserNotification
from Mainapp.notifications import bulk_create_user_notifications


class NotificationListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="notif@example.com", username="notif@example.com", password="Testpass123!",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_bulk_create_commits_in_small_chunks(self):
        notifications = [UserNotification(user=self.user, title="t%d" % i, message="m") for i in range(1203)]
        manager = UserNotification.objects
        with patch.object(manager, "bulk_create", wraps=manager.bulk_create) as bulk_create:
            bulk_create_user_notifications(notifications)
        self.assertEqual([len(call.args[0]) for call in bulk_create.call_args_list], [500, 500, 203])
        self.assertEqual(UserNotification.objects.filter(user=self.user).count(), 1203)

    def test_list_response_shape_and_filters(self):
        bulk_create_user_notifications(
            [UserNotification(user=self.user, title="unread %d" % i, message="m") for i in range(25)]
            + [UserNotification(user=self.user, title="read", message="m", is_read=True)]
            + [UserNotification(user=self.user, title="deleted", message="m", is_deleted=True)]
        )

        response = self.client.get("/api/dashboard/notifications/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 25)
        self.assertEqual(len(body["results"]), 20)
        self.assertEqual(
            set(body["results"][0]),
            {"id", "title", "message", "notification_type", "category", "is_read", "redirect_url", "created_at", "read_at"},
        )

        self.assertEqual(self.client.get("/api/dashboard/notifications/?is_read=true").json()["count"], 1)
        self.assertEqual(self.client.get("/api/dashboard/notifications/?is_read=all").json()["count"], 26)
