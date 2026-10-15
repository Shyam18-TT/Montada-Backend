"""
Push pipeline contract tests: message shape per category, device registration, logout,
send-error handling, recipient resolution, delivery log and cleanup.

The FCM client is faked (``messaging.send_each_for_multicast``); nothing leaves the machine.
"""
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from firebase_admin import _messaging_encoder, messaging
from firebase_admin import exceptions as fb_exceptions
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

import firebase
import push_contract
from Mainapp.models import DeviceToken, PushDelivery, User, hash_push_token
from Mainapp.notification_preferences import Category, save_user_modes


def make_user(email, **extra):
    return User.objects.create_user(email=email, username=email, password="Testpass123!", **extra)


def ok_response(*message_ids):
    return SimpleNamespace(
        success_count=len(message_ids), failure_count=0,
        responses=[SimpleNamespace(success=True, exception=None, message_id=m) for m in message_ids],
    )


def failed(exc):
    return SimpleNamespace(success=False, exception=exc, message_id=None)


def multicast_response(*responses):
    ok = sum(1 for r in responses if r.success)
    return SimpleNamespace(success_count=ok, failure_count=len(responses) - ok, responses=list(responses))


def encode(multicast, token="device-token"):
    """The FCM v1 JSON the SDK would post for one token of a multicast."""
    message = messaging.Message(
        token=token,
        notification=multicast.notification,
        android=multicast.android,
        apns=multicast.apns,
        data=multicast.data,
    )
    return _messaging_encoder.MessageEncoder().default(message)


def send_and_encode(data, *, sound=True, platform="unknown", title="Title", body="Body"):
    with patch("firebase.messaging.send_each_for_multicast", return_value=ok_response("m1")) as send_each:
        firebase.send_push_to_tokens(
            tokens=["device-token"], title=title, body=body, data=data, sound=sound, platform=platform,
        )
    return encode(send_each.call_args.args[0])


# (data, android channel sound, android channel silent, ios thread, ttl)
CONTRACT_ROWS = {
    "user_price_alert": ({"type": "user_price_alert"}, "montada_price_alerts_v2",
                         "montada_price_alerts_silent", "price_alerts", 3600),
    "market_percentage_alert": ({"type": "market_percentage_alert"}, "montada_price_alerts_v2",
                                "montada_price_alerts_silent", "price_alerts", 3600),
    "signal_published": ({"type": "signal_published"}, "montada_trade_ideas_v2",
                         "montada_trade_ideas_silent", "trade_ideas", 3600),
    "signal_closed": ({"type": "signal_closed"}, "montada_trade_ideas_v2",
                      "montada_trade_ideas_silent", "trade_ideas", 3600),
    "signal_alert": ({"type": "signal_alert"}, "montada_trade_ideas_v2",
                     "montada_trade_ideas_silent", "trade_ideas", 3600),
    "economic_event_high": ({"type": "economic_event", "importance": "high"}, "montada_economic_high_v2",
                            "montada_economic_high_silent", "economic", 21600),
    "economic_event_medium": ({"type": "economic_event", "importance": "medium"}, "montada_economic_v2",
                              "montada_economic_silent", "economic", 21600),
    "economic_reminder": ({"type": "economic_reminder"}, "montada_economic_reminders_v2",
                          "montada_economic_reminders_silent", "reminders", 3600),
    "economic_global_reminder": ({"type": "economic_global_reminder"}, "montada_economic_reminders_v2",
                                 "montada_economic_reminders_silent", "reminders", 3600),
    "chat_message": ({"type": "chat_message"}, "montada_chat", "montada_chat_silent", "chat", 86400),
    "admin_broadcast": ({"type": "admin_broadcast"}, "montada_broadcasts",
                        "montada_broadcasts_silent", "broadcasts", 259200),
    "other": ({"type": "new_follower"}, "montada_notifications", "montada_notifications_silent", "general", 21600),
}


class MessageShapeTests(TestCase):
    def test_sound_mode_android_and_ios_for_every_contract_row(self):
        for name, (data, channel, _silent, thread, ttl) in CONTRACT_ROWS.items():
            with self.subTest(name):
                encoded = send_and_encode(dict(data))
                android = encoded["android"]
                self.assertEqual(android["priority"], "high")
                self.assertEqual(android["ttl"], "%ds" % ttl)
                self.assertEqual(android["notification"]["channel_id"], channel)
                self.assertEqual(android["notification"]["sound"], "montada_push")
                self.assertFalse(android["notification"]["default_sound"])
                self.assertEqual(android["notification"]["notification_priority"], "PRIORITY_HIGH")
                self.assertEqual(android["notification"]["visibility"], "PUBLIC")
                self.assertEqual(
                    android["notification"]["tag"], encoded["data"]["notification_id"],
                )

                headers = encoded["apns"]["headers"]
                self.assertEqual(headers["apns-push-type"], "alert")
                self.assertEqual(headers["apns-priority"], "10")
                self.assertEqual(headers["apns-collapse-id"], encoded["data"]["notification_id"])
                self.assertGreater(int(headers["apns-expiration"]), int(timezone.now().timestamp()))
                self.assertLessEqual(int(headers["apns-expiration"]), int(timezone.now().timestamp()) + ttl + 5)
                aps = encoded["apns"]["payload"]["aps"]
                self.assertEqual(aps["sound"], "montada_push.wav")
                self.assertEqual(aps["mutable-content"], 1)
                self.assertEqual(aps["content-available"], 1)
                self.assertEqual(aps["thread-id"], thread)
                self.assertEqual(aps["interruption-level"], "active")
                self.assertNotIn("badge", aps)
                self.assertNotIn("apns-topic", headers)

    def test_silent_mode_uses_silent_channel_and_no_sound(self):
        for name, (data, _channel, silent_channel, _thread, _ttl) in CONTRACT_ROWS.items():
            with self.subTest(name):
                encoded = send_and_encode(dict(data), sound=False)
                android = encoded["android"]["notification"]
                self.assertEqual(android["channel_id"], silent_channel)
                self.assertNotIn("sound", android)
                self.assertFalse(android["default_sound"])
                self.assertNotIn("sound", encoded["apns"]["payload"]["aps"])
                self.assertEqual(encoded["data"]["silent"], "true")

    def test_news_never_plays_a_sound(self):
        for sound in (True, False):
            encoded = send_and_encode({"type": "news_update"}, sound=sound)
            self.assertEqual(encoded["android"]["notification"]["channel_id"], "montada_news" + ("" if sound else "_silent"))
            self.assertNotIn("sound", encoded["android"]["notification"])
            self.assertNotIn("sound", encoded["apns"]["payload"]["aps"])
        self.assertEqual(send_and_encode({"type": "news_update"})["android"]["ttl"], "21600s")

    def test_no_pre_v2_channel_or_unshipped_sound_is_ever_sent(self):
        removed = {"montada_price_alerts", "montada_trade_ideas", "montada_economic",
                   "montada_economic_high", "montada_economic_reminders"}
        for name, (data, *_rest) in CONTRACT_ROWS.items():
            encoded = send_and_encode(dict(data))
            self.assertNotIn(encoded["android"]["notification"]["channel_id"], removed, name)
            self.assertEqual(encoded["android"]["notification"]["sound"], "montada_push", name)

    def test_data_fields_are_strings_and_always_present(self):
        encoded = send_and_encode(
            {"type": "user_price_alert", "alert_id": 7, "current_price": 65012.4, "notification_category": "PRICE_ALERTS"},
            title="BTCUSD price alert", body="crossed",
        )
        data = encoded["data"]
        self.assertTrue(all(isinstance(v, str) for v in data.values()))
        for key in ("type", "notification_id", "preference_category", "title", "body", "silent", "sent_at"):
            self.assertIn(key, data)
        self.assertEqual(data["preference_category"], "PRICE_ALERTS")
        self.assertEqual(data["silent"], "false")
        self.assertEqual(data["alert_id"], "7")
        self.assertEqual(data["title"], "BTCUSD price alert")
        self.assertNotIn("category", data)  # data.category belongs to the app's own use

    def test_platform_blocks(self):
        both = send_and_encode({"type": "user_price_alert"}, platform="unknown")
        self.assertIn("android", both)
        self.assertIn("apns", both)
        android_only = send_and_encode({"type": "user_price_alert"}, platform="android")
        self.assertIn("android", android_only)
        self.assertNotIn("apns", android_only)
        ios_only = send_and_encode({"type": "user_price_alert"}, platform="ios")
        self.assertIn("apns", ios_only)
        self.assertNotIn("android", ios_only)

    def test_oversized_payload_is_trimmed_below_4kb(self):
        data = push_contract.build_data(
            {"type": "market_percentage_alert", "signal_ids": ",".join(["x" * 36] * 200)},
            title="t", body="b",
        )
        self.assertLess(push_contract._payload_bytes(data), 4096)
        self.assertNotIn("signal_ids", data)


class RegistrationTests(TestCase):
    def setUp(self):
        self.user = make_user("reg@example.com")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("Mainapp:save_fcm_token")

    def post(self, **body):
        return self.client.post(self.url, body, format="json")

    def test_two_devices_for_one_user_make_two_rows_and_both_receive(self):
        self.post(token="tok-a", device_id="dev-a", platform="android")
        self.post(token="tok-b", device_id="dev-b", platform="ios")
        self.assertEqual(DeviceToken.objects.filter(user=self.user).count(), 2)
        tokens = {t.token for t in firebase.get_push_targets_for_users([self.user])}
        self.assertEqual(tokens, {"tok-a", "tok-b"})

    def test_same_device_new_user_moves_the_row(self):
        other = make_user("other-reg@example.com")
        self.post(token="tok-a", device_id="dev-a", platform="android")
        client = APIClient()
        client.force_authenticate(other)
        res = client.post(self.url, {"token": "tok-a2", "device_id": "dev-a", "platform": "android"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(DeviceToken.objects.filter(device_id="dev-a").count(), 1)
        self.assertEqual(DeviceToken.objects.get(device_id="dev-a").user, other)
        self.assertEqual(firebase.get_push_targets_for_users([self.user]), [])

    def test_token_rotation_updates_the_row_in_place(self):
        first = self.post(token="tok-old", device_id="dev-a", platform="android").json()
        second = self.post(token="tok-new", device_id="dev-a", platform="android").json()
        self.assertEqual(first["token_id"], second["token_id"])
        row = DeviceToken.objects.get(device_id="dev-a")
        self.assertEqual(row.fcm_token, "tok-new")
        self.assertEqual(row.token_hash, hash_push_token("tok-new"))

    def test_same_call_ten_times_leaves_one_row(self):
        for _ in range(10):
            res = self.post(token="tok-a", device_id="dev-a", platform="android")
            self.assertEqual(res.status_code, 200)
        self.assertEqual(DeviceToken.objects.count(), 1)
        body = res.json()
        self.assertTrue(body["registered"])
        self.assertEqual(body["device_id"], "dev-a")
        self.assertEqual(body["platform"], "android")

    def test_missing_device_id_upserts_by_token(self):
        self.post(token="tok-a")
        self.post(token="tok-a")
        self.assertEqual(DeviceToken.objects.count(), 1)
        self.assertEqual(DeviceToken.objects.get().platform, "unknown")

    def test_token_moved_to_another_device_deletes_the_old_registration(self):
        self.post(token="tok-a", device_id="dev-old", platform="android")
        self.post(token="tok-a", device_id="dev-new", platform="android")
        self.assertEqual(list(DeviceToken.objects.values_list("device_id", flat=True)), ["dev-new"])

    def test_unknown_fields_and_platform_are_tolerated(self):
        res = self.post(token="tok-a", device_id="dev-a", platform="Symbian", app_version="1.2.9", extra={"x": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(DeviceToken.objects.get().platform, "unknown")

    def test_old_build_without_platform_keeps_known_platform(self):
        self.post(token="tok-a", device_id="dev-a", platform="ios")
        self.post(token="tok-a", device_id="dev-a")
        self.assertEqual(DeviceToken.objects.get().platform, "ios")

    def test_invalid_requests_are_400(self):
        self.assertEqual(self.post(token="   ").status_code, 400)
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.post(token="x" * 5000).status_code, 400)

    def test_registration_reactivates_a_deactivated_device(self):
        self.post(token="tok-a", device_id="dev-a", platform="android")
        DeviceToken.objects.update(is_active=False, failure_count=5, last_error="UNAVAILABLE")
        self.post(token="tok-a", device_id="dev-a", platform="android")
        row = DeviceToken.objects.get()
        self.assertTrue(row.is_active)
        self.assertEqual((row.failure_count, row.last_error), (0, ""))


class LogoutTests(TestCase):
    def setUp(self):
        self.user = make_user("logout@example.com")
        self.other = make_user("logout-other@example.com")
        self.client = APIClient()  # no credentials: the access token may be expired or missing
        self.url = reverse("Mainapp:logout")
        DeviceToken.objects.create(user=self.user, fcm_token="tok-a", device_id="dev-a")
        DeviceToken.objects.create(user=self.other, fcm_token="tok-b", device_id="dev-b")

    def refresh_for(self, user):
        return str(RefreshToken.for_user(user))

    def test_expired_access_token_still_removes_the_device(self):
        res = self.client.post(
            self.url, {"refresh_token": self.refresh_for(self.user), "device_id": "dev-a"},
            format="json", HTTP_AUTHORIZATION="Bearer this.is.expired",
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(DeviceToken.objects.filter(device_id="dev-a").exists())
        self.assertTrue(DeviceToken.objects.filter(device_id="dev-b").exists())

    def test_another_users_device_id_deletes_nothing(self):
        res = self.client.post(
            self.url, {"refresh_token": self.refresh_for(self.user), "device_id": "dev-b"}, format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(DeviceToken.objects.filter(device_id="dev-b").exists())

    def test_no_device_id_only_blacklists_the_refresh_token(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

        res = self.client.post(self.url, {"refresh_token": self.refresh_for(self.user)}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(DeviceToken.objects.count(), 2)
        self.assertEqual(BlacklistedToken.objects.count(), 1)

    def test_replayed_logout_with_blacklisted_refresh_token_still_succeeds(self):
        refresh = self.refresh_for(self.user)
        for _ in range(2):
            res = self.client.post(self.url, {"refresh_token": refresh, "device_id": "dev-a"}, format="json")
            self.assertEqual(res.status_code, 200)

    def test_invalid_refresh_token_is_401_and_removes_nothing(self):
        res = self.client.post(self.url, {"refresh_token": "garbage", "device_id": "dev-a"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(DeviceToken.objects.count(), 2)

    def test_missing_refresh_token_is_400(self):
        res = self.client.post(self.url, {"device_id": "dev-a"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(DeviceToken.objects.count(), 2)


class AccountEventTests(TestCase):
    def setUp(self):
        self.user = make_user("events@example.com", is_verified=True)
        DeviceToken.objects.create(user=self.user, fcm_token="tok-a", device_id="dev-a")
        DeviceToken.objects.create(user=self.user, fcm_token="tok-b", device_id="dev-b")

    def test_password_change_removes_every_device(self):
        client = APIClient()
        client.force_authenticate(self.user)
        res = client.post(
            reverse("Mainapp:change_password"),
            {"old_password": "Testpass123!", "new_password": "Newpass456!?", "new_password2": "Newpass456!?"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(DeviceToken.objects.filter(user=self.user).count(), 0)

    def test_banned_user_loses_devices_and_is_skipped_when_sending(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(DeviceToken.objects.filter(user=self.user).count(), 0)

    def test_soft_deleted_user_loses_devices(self):
        self.user.is_soft_deleted = True
        self.user.save()
        self.assertEqual(DeviceToken.objects.filter(user=self.user).count(), 0)

    def test_inactive_user_is_never_a_recipient_even_with_rows(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)  # bypasses the signal
        self.assertEqual(firebase.get_push_targets_for_users([self.user]), [])


@override_settings(FCM_PUSH_ENABLED=True)
class SendErrorTests(TestCase):
    def setUp(self):
        self.user = make_user("errors@example.com")
        for i in range(3):
            DeviceToken.objects.create(user=self.user, fcm_token="tok-%d" % i, device_id="dev-%d" % i)

    def send(self, responses_per_call):
        calls = iter(responses_per_call)
        with patch("firebase.messaging.send_each_for_multicast", side_effect=lambda m: next(calls)) as send_each, \
                patch("firebase._sleep") as sleep:
            result = firebase.send_push_to_users(
                [self.user], "t", "b", data={"type": "user_price_alert"},
            )
        return result, send_each, sleep

    def tokens(self):
        return set(DeviceToken.objects.values_list("fcm_token", flat=True))

    def sent_tokens(self, send_each):
        return [sorted(call.args[0].tokens) for call in send_each.call_args_list]

    def test_unregistered_sender_mismatch_and_malformed_token_are_deleted(self):
        responses = multicast_response(
            failed(messaging.UnregisteredError("gone")),
            failed(messaging.SenderIdMismatchError("other project")),
            failed(fb_exceptions.InvalidArgumentError("The registration token is not a valid FCM registration token")),
        )
        result, _send, _sleep = self.send([responses])
        self.assertEqual(result["failure_count"], 3)
        self.assertEqual(self.tokens(), set())
        self.assertEqual(PushDelivery.objects.filter(status="deleted_token").count(), 3)

    def test_payload_invalid_argument_keeps_the_row_and_logs_the_keys(self):
        responses = multicast_response(
            *[failed(fb_exceptions.InvalidArgumentError("Invalid JSON payload: unknown field")) for _ in range(3)]
        )
        with self.assertLogs("firebase", level="ERROR") as logs:
            self.send([responses])
        self.assertEqual(len(self.tokens()), 3)
        self.assertTrue(any("keys=" in line and "notification_id" in line for line in logs.output))

    def test_third_party_auth_error_keeps_rows_and_alerts(self):
        responses = multicast_response(*[failed(messaging.ThirdPartyAuthError("apns")) for _ in range(3)])
        with self.assertLogs("firebase", level="ERROR") as logs:
            self.send([responses])
        self.assertEqual(len(self.tokens()), 3)
        self.assertTrue(any("THIRD_PARTY_AUTH_ERROR" in line for line in logs.output))

    def test_unavailable_is_retried_with_backoff_and_row_kept(self):
        first = multicast_response(
            SimpleNamespace(success=True, exception=None, message_id="m0"),
            failed(fb_exceptions.UnavailableError("503")),
            failed(messaging.QuotaExceededError("429")),
        )
        second = multicast_response(
            SimpleNamespace(success=True, exception=None, message_id="m1"),
            SimpleNamespace(success=True, exception=None, message_id="m2"),
        )
        result, send_each, sleep = self.send([first, second])
        self.assertEqual(send_each.call_count, 2)
        self.assertEqual(len(self.sent_tokens(send_each)[1]), 2)  # only the two transient ones are retried
        sleep.assert_called_once()
        self.assertEqual((result["success_count"], result["failure_count"]), (3, 0))
        self.assertEqual(len(self.tokens()), 3)

    def test_retries_are_capped_and_failures_are_counted(self):
        def always_unavailable(count):
            return multicast_response(*[failed(fb_exceptions.UnavailableError("503")) for _ in range(count)])

        responses = [always_unavailable(3)] + [always_unavailable(3) for _ in range(3)]
        result, send_each, sleep = self.send(responses)
        self.assertEqual(send_each.call_count, 4)  # first attempt + 3 retries
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(result["failure_count"], 3)
        self.assertEqual(len(self.tokens()), 3)
        self.assertEqual(set(DeviceToken.objects.values_list("failure_count", flat=True)), {1})
        self.assertEqual(set(DeviceToken.objects.values_list("last_error", flat=True)), {"UNAVAILABLE"})

    def test_five_consecutive_transient_failures_deactivate_the_device(self):
        DeviceToken.objects.update(failure_count=4)
        responses = [multicast_response(*[failed(fb_exceptions.UnavailableError("503")) for _ in range(3)])
                     for _ in range(4)]
        self.send(responses)
        self.assertEqual(DeviceToken.objects.filter(is_active=False).count(), 3)
        self.assertEqual(firebase.get_push_targets_for_users([self.user]), [])

    def test_one_bad_token_does_not_stop_the_others_and_success_is_recorded(self):
        responses = multicast_response(
            SimpleNamespace(success=True, exception=None, message_id="projects/x/messages/1"),
            failed(messaging.UnregisteredError("gone")),
            SimpleNamespace(success=True, exception=None, message_id="projects/x/messages/2"),
        )
        result, _send, _sleep = self.send([responses])
        self.assertEqual((result["success_count"], result["failure_count"]), (2, 1))
        self.assertEqual(len(self.tokens()), 2)
        self.assertEqual(DeviceToken.objects.filter(last_success_at__isnull=False).count(), 2)
        self.assertEqual(
            set(PushDelivery.objects.filter(status="sent").values_list("fcm_message_id", flat=True)),
            {"projects/x/messages/1", "projects/x/messages/2"},
        )

    def test_whole_call_failing_does_not_raise(self):
        with patch("firebase.messaging.send_each_for_multicast", side_effect=ConnectionError("down")), \
                patch("firebase._sleep"):
            result = firebase.send_push_to_users([self.user], "t", "b", data={"type": "user_price_alert"})
        self.assertEqual(result["failure_count"], 3)
        self.assertEqual(len(self.tokens()), 3)


@override_settings(FCM_PUSH_ENABLED=True)
class RecipientTests(TestCase):
    def setUp(self):
        self.user = make_user("recipient@example.com")

    def test_newer_updated_at_wins_for_rows_of_one_device(self):
        # Two rows for one physical device cannot coexist any more, so build them without the
        # constraint's help: the resolver must still prefer the newest.
        old = DeviceToken.objects.create(user=self.user, fcm_token="tok-old", device_id="dev-1")
        new = DeviceToken.objects.create(user=self.user, fcm_token="tok-new", device_id="dev-2")
        DeviceToken.objects.filter(pk=old.pk).update(device_id="DEV-X", updated_at=timezone.now() - timedelta(days=3))
        DeviceToken.objects.filter(pk=new.pk).update(device_id="dev-x", updated_at=timezone.now())
        # device ids differ only by case; SQLite compares them case-sensitively, SQL Server does not.
        targets = firebase.get_push_targets_for_users([self.user])
        self.assertEqual([t.token for t in targets], ["tok-new"])

    def test_off_is_not_sent_silent_is_flagged_and_default_is_sound(self):
        quiet = make_user("quiet@example.com")
        muted = make_user("muted@example.com")
        for user, token in ((self.user, "tok-loud"), (quiet, "tok-quiet"), (muted, "tok-muted")):
            DeviceToken.objects.create(user=user, fcm_token=token, device_id=token)
        save_user_modes(quiet, {Category.PRICE_ALERTS: "silent"})
        save_user_modes(muted, {Category.PRICE_ALERTS: "off"})

        targets = firebase.get_push_targets_for_users([self.user, quiet, muted], Category.PRICE_ALERTS)
        modes = {t.token: t.mode for t in targets}
        self.assertEqual(modes, {"tok-loud": "sound", "tok-quiet": "silent"})

        # A user with no saved preferences gets sound for every category.
        for category in (Category.PRICE_ALERTS, Category.NEWS, Category.TRADE_IDEAS, Category.MESSAGES):
            self.assertEqual(firebase.get_push_targets_for_users([self.user], category)[0].mode, "sound")

    def test_silent_user_gets_a_silent_payload_and_off_user_is_logged_as_skipped(self):
        quiet = make_user("quiet2@example.com")
        muted = make_user("muted2@example.com")
        for user, token in ((quiet, "tok-quiet"), (muted, "tok-muted")):
            DeviceToken.objects.create(user=user, fcm_token=token, device_id=token)
        save_user_modes(quiet, {Category.PRICE_ALERTS: "silent"})
        save_user_modes(muted, {Category.PRICE_ALERTS: "off"})

        with patch("firebase.messaging.send_each_for_multicast", return_value=ok_response("m1")) as send_each:
            firebase.send_push_to_users([quiet, muted], "t", "b", data={"type": "user_price_alert"})
        self.assertEqual(send_each.call_count, 1)
        message = send_each.call_args.args[0]
        self.assertEqual(message.tokens, ["tok-quiet"])
        encoded = encode(message, "tok-quiet")
        self.assertEqual(encoded["android"]["notification"]["channel_id"], "montada_price_alerts_silent")
        self.assertEqual(encoded["data"]["silent"], "true")
        self.assertEqual(PushDelivery.objects.filter(status="skipped_pref", user_id=muted.pk).count(), 1)

    def test_rows_without_device_id_are_grouped_by_token(self):
        DeviceToken.objects.create(user=self.user, fcm_token="tok-1")
        DeviceToken.objects.create(user=self.user, fcm_token="tok-2")
        self.assertEqual(len(firebase.get_push_targets_for_users([self.user])), 2)

    def test_android_and_ios_devices_get_their_own_block(self):
        DeviceToken.objects.create(user=self.user, fcm_token="tok-and", device_id="a", platform="android")
        DeviceToken.objects.create(user=self.user, fcm_token="tok-ios", device_id="b", platform="ios")
        with patch("firebase.messaging.send_each_for_multicast", return_value=ok_response("m1")) as send_each:
            firebase.send_push_to_users([self.user], "t", "b", data={"type": "user_price_alert"})
        blocks = {tuple(c.args[0].tokens): (c.args[0].android is not None, c.args[0].apns is not None)
                  for c in send_each.call_args_list}
        self.assertEqual(blocks, {("tok-and",): (True, False), ("tok-ios",): (False, True)})


class ProducerContractTests(TestCase):
    def test_every_push_type_in_the_contract_maps_to_a_known_preference_category(self):
        from Mainapp.notification_preferences import category_for_push

        expected = {
            "user_price_alert": Category.PRICE_ALERTS,
            "market_percentage_alert": Category.SYMBOL_MOVES,
            "signal_published": Category.TRADE_IDEAS,
            "economic_event": Category.ECONOMIC_EVENTS,
            "economic_reminder": Category.ECONOMIC_REMINDERS,
            "news_update": Category.NEWS,
            "chat_message": Category.MESSAGES,
        }
        for push_type, category in expected.items():
            self.assertEqual(category_for_push({"type": push_type}), category, push_type)

    def test_sample_payloads_cover_the_contract_types(self):
        for push_type, sample in push_contract.SAMPLE_PAYLOADS.items():
            data = push_contract.build_data(
                dict(sample["data"], type=push_type), title=sample["title"], body=sample["body"],
            )
            self.assertTrue(all(isinstance(v, str) for v in data.values()), push_type)


@override_settings(FCM_PUSH_ENABLED=True)
class PriceAlertFlowTests(TestCase):
    def setUp(self):
        from Signals.models import AssetClass, Instrument, PriceAlert

        self.user = make_user("alert@example.com")
        DeviceToken.objects.create(user=self.user, fcm_token="tok-a", device_id="dev-a", platform="android")
        asset_class = AssetClass.objects.create(name="Crypto")
        self.instrument = Instrument.objects.create(asset_class=asset_class, symbol="BTCUSD", name="Bitcoin")
        self.PriceAlert = PriceAlert

    def _alert(self):
        return self.PriceAlert.objects.create(
            user=self.user, instrument=self.instrument, target_price="65000", condition="above",
        )

    def test_inbox_row_is_created_first_and_push_uses_the_contract(self):
        from Mainapp.models import UserNotification
        from Signals.management.commands.run_price_alerts import _trigger_user_price_alert

        alert = self._alert()
        with patch("firebase.messaging.send_each_for_multicast", return_value=ok_response("m1")) as send_each:
            with self.captureOnCommitCallbacks(execute=True):
                _trigger_user_price_alert(alert, 65012.4000000001)

        alert.refresh_from_db()
        self.assertTrue(alert.is_triggered)
        inbox = UserNotification.objects.get(user=self.user, category="PRICE_ALERT")
        self.assertEqual(inbox.title, "BTCUSD price alert")
        self.assertEqual(inbox.message, "BTCUSD crossed above 65000.00 (now 65012.40)")

        encoded = encode(send_each.call_args.args[0], "tok-a")
        self.assertEqual(encoded["data"]["type"], "user_price_alert")
        self.assertEqual(encoded["data"]["notification_id"], str(inbox.pk))
        self.assertEqual(encoded["data"]["current_price"], "65012.40")
        self.assertEqual(encoded["data"]["target_price"], "65000.00")
        self.assertEqual(encoded["android"]["notification"]["channel_id"], "montada_price_alerts_v2")

    def test_alert_stays_armed_when_the_inbox_row_cannot_be_created(self):
        from Signals.management.commands.run_price_alerts import _trigger_user_price_alert

        alert = self._alert()
        with patch("Mainapp.models.UserNotification.objects.create", side_effect=RuntimeError("db down")), \
                patch("firebase.messaging.send_each_for_multicast") as send_each:
            with self.captureOnCommitCallbacks(execute=True):
                _trigger_user_price_alert(alert, 65100)
        alert.refresh_from_db()
        self.assertFalse(alert.is_triggered)
        send_each.assert_not_called()

    def test_failed_push_does_not_lose_the_alert_or_duplicate_the_inbox_row(self):
        from Mainapp.models import UserNotification
        from Signals.management.commands.run_price_alerts import _trigger_user_price_alert

        alert = self._alert()
        with patch("firebase.messaging.send_each_for_multicast", side_effect=ConnectionError("down")), \
                patch("firebase._sleep"):
            with self.captureOnCommitCallbacks(execute=True):
                _trigger_user_price_alert(alert, 65100)
            with self.captureOnCommitCallbacks(execute=True):
                _trigger_user_price_alert(alert, 65100)  # next check: already triggered, no second row
        alert.refresh_from_db()
        self.assertTrue(alert.is_triggered)
        self.assertEqual(UserNotification.objects.filter(user=self.user, category="PRICE_ALERT").count(), 1)


class CleanupAndToolsTests(TestCase):
    def setUp(self):
        self.user = make_user("cleanup@example.com")

    def test_cleanup_removes_devices_not_seen_for_60_days_and_old_log_rows(self):
        fresh = DeviceToken.objects.create(user=self.user, fcm_token="tok-fresh", device_id="fresh")
        stale = DeviceToken.objects.create(user=self.user, fcm_token="tok-stale", device_id="stale")
        DeviceToken.objects.filter(pk=stale.pk).update(last_seen_at=timezone.now() - timedelta(days=61))
        recent_log = PushDelivery.objects.create(status="sent", push_type="x")
        old_log = PushDelivery.objects.create(status="sent", push_type="x")
        PushDelivery.objects.filter(pk=old_log.pk).update(created_at=timezone.now() - timedelta(days=31))

        out = StringIO()
        call_command("cleanup_push_devices", stdout=out)

        self.assertEqual(list(DeviceToken.objects.values_list("pk", flat=True)), [fresh.pk])
        self.assertEqual(list(PushDelivery.objects.values_list("pk", flat=True)), [recent_log.pk])
        self.assertIn("'stale_devices': 1", out.getvalue())

    def test_cleanup_dry_run_changes_nothing(self):
        stale = DeviceToken.objects.create(user=self.user, fcm_token="tok-stale", device_id="stale")
        DeviceToken.objects.filter(pk=stale.pk).update(last_seen_at=timezone.now() - timedelta(days=90))
        call_command("cleanup_push_devices", "--dry-run", stdout=StringIO())
        self.assertEqual(DeviceToken.objects.count(), 1)

    def test_send_test_push_sends_to_every_device_only_with_yes(self):
        DeviceToken.objects.create(user=self.user, fcm_token="tok-a", device_id="a", platform="android")
        DeviceToken.objects.create(user=self.user, fcm_token="tok-b", device_id="b", platform="ios")
        with override_settings(FCM_PUSH_ENABLED=True), \
                patch("firebase.messaging.send_each_for_multicast", return_value=ok_response("m1")) as send_each:
            out = StringIO()
            call_command("send_test_push", "cleanup@example.com", "user_price_alert", stdout=out)
            send_each.assert_not_called()  # dry run
            call_command("send_test_push", "cleanup@example.com", "user_price_alert", "--yes", "--silent", stdout=out)
        self.assertEqual(send_each.call_count, 2)
        first = encode(send_each.call_args_list[0].args[0], "x")
        self.assertEqual(first["data"]["type"], "user_price_alert")
        self.assertEqual(first["data"]["silent"], "true")
        self.assertIn("OK", out.getvalue())
