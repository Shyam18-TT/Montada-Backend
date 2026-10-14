import logging

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase
from rest_framework.test import APIClient

from Mainapp.audit import audit, client_ip

User = get_user_model()


class AuditHelperTests(SimpleTestCase):
    def test_formats_key_values_and_skips_none(self):
        with self.assertLogs("audit", level="INFO") as logs:
            audit("thing.happened", actor="system", signal="abc", note="has space", skipped=None)
        line = logs.records[0].getMessage()
        self.assertIn("action=thing.happened", line)
        self.assertIn("actor=system", line)
        self.assertIn("signal=abc", line)
        self.assertIn('note="has space"', line)
        self.assertNotIn("skipped", line)

    def test_secret_like_keys_are_redacted(self):
        with self.assertLogs("audit", level="INFO") as logs:
            audit("x", password="hunter2", otp="123456", fcm_token="tok", payment_intent="pi_1")
        line = logs.records[0].getMessage()
        for secret in ("hunter2", "123456", "tok "):
            self.assertNotIn(secret, line)
        self.assertIn("password=[redacted]", line)
        self.assertIn("payment_intent=pi_1", line)

    def test_multiline_values_stay_on_one_line(self):
        with self.assertLogs("audit", level="INFO") as logs:
            audit("x", reason="line1\nline2")
        self.assertNotIn("\n", logs.records[0].getMessage())

    def test_never_raises(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("nope")

        with self.assertLogs("app.exceptions", level="ERROR"):
            audit("x", value=Boom())

    def test_client_ip_prefers_forwarded_for(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8", REMOTE_ADDR="9.9.9.9")
        self.assertEqual(client_ip(request), "1.2.3.4")
        self.assertIsNone(client_ip(None))


class RequestAuditFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="a@example.com", username="a@example.com", password="Testpass123!", is_verified=True,
        )
        self.client = APIClient()

    def test_failed_login_is_audited_without_password(self):
        with self.assertLogs("audit", level="INFO") as logs:
            res = self.client.post(
                "/api/auth/login/", {"email": "a@example.com", "password": "wrong-password"}, format="json"
            )
        self.assertEqual(res.status_code, 400)
        lines = [r.getMessage() for r in logs.records]
        self.assertTrue(any("action=auth.login_failed" in l and "email=a@example.com" in l for l in lines))
        self.assertFalse(any("wrong-password" in l for l in lines))
        # request-level line for the same call
        self.assertTrue(any(l.startswith("request ") and "status=400" in l for l in lines))

    def test_successful_login_is_audited_with_actor(self):
        with self.assertLogs("audit", level="INFO") as logs:
            res = self.client.post(
                "/api/auth/login/", {"email": "a@example.com", "password": "Testpass123!"}, format="json"
            )
        self.assertEqual(res.status_code, 200)
        lines = [r.getMessage() for r in logs.records]
        self.assertTrue(any(
            "action=auth.login_success" in l and f"actor={self.user.pk}" in l for l in lines
        ))

    def test_authenticated_mutation_logs_jwt_user_id(self):
        self.client.force_authenticate(self.user)
        with self.assertLogs("audit.requests", level="INFO") as logs:
            self.client.post("/api/auth/logout/", {}, format="json")
        line = logs.records[0].getMessage()
        self.assertIn("method=POST", line)
        self.assertIn(f"user={self.user.pk}", line)

    def test_plain_successful_get_is_not_logged(self):
        self.client.force_authenticate(self.user)
        with self.assertNoLogs("audit.requests", level="INFO"):
            self.client.get("/api/auth/profile/")
