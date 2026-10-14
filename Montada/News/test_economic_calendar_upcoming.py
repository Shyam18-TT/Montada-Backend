from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from News.models import EconomicCalendarEvent

User = get_user_model()


class EconomicCalendarUpcomingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="cal@example.com", username="cal@example.com", password="Testpass123!", is_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("News:economic_calendar")

        now = timezone.now()
        self.past = self._event(1, "Past", now - timedelta(hours=3))
        self.soon = self._event(2, "Soon", now + timedelta(hours=1))
        self.later = self._event(3, "Later", now + timedelta(days=5), importance="high")
        self.far = self._event(4, "Far future", now + timedelta(days=40))

    def _event(self, provider_id, name, when, importance="low"):
        return EconomicCalendarEvent.objects.create(
            provider_id=provider_id, event_name=name, release_date=when, importance=importance,
        )

    def _names(self, **params):
        res = self.client.get(self.url, params)
        self.assertEqual(res.status_code, 200)
        return [item["event_name"] for item in res.data["results"]]

    def test_without_param_includes_recent_past(self):
        self.assertEqual(self._names(), ["Past", "Soon", "Later"])

    def test_upcoming_only_returns_events_after_now(self):
        for value in ("true", "True", "1", "yes"):
            cache.clear()
            self.assertEqual(self._names(upcoming=value), ["Soon", "Later"], value)

    def test_upcoming_false_or_garbage_is_ignored(self):
        for value in ("false", "0", "maybe", ""):
            cache.clear()
            self.assertEqual(self._names(upcoming=value), ["Past", "Soon", "Later"], value)

    def test_upcoming_combines_with_other_filters(self):
        self.assertEqual(self._names(upcoming="true", importance="high"), ["Later"])

    def test_upcoming_with_date_from_still_excludes_past(self):
        # An explicit date_from replaces the default 20-day window (existing behaviour), so
        # the far-future event is included; the past one is still excluded by "upcoming".
        date_from = (timezone.now() - timedelta(days=2)).isoformat()
        self.assertEqual(
            self._names(upcoming="true", date_from=date_from), ["Soon", "Later", "Far future"],
        )
