import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from Followers.models import Follow

from .models import ChatMessage, Conversation, ConversationParticipant

User = get_user_model()


def _user(name, user_type="trader"):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", name=name.title(), user_type=user_type
    )


def _follow(follower, followed):
    return Follow.objects.create(
        follower=follower, followed=followed, status=Follow.Status.ACCEPTED, is_active=True
    )


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class GroupChatTests(APITestCase):
    def setUp(self):
        self.analyst = _user("ana", "analyst")
        self.follower = _user("fol")          # follows the analyst
        self.followed = _user("fed")          # the analyst follows them
        self.stranger = _user("str")
        _follow(self.follower, self.analyst)
        _follow(self.analyst, self.followed)
        # Never touch Redis/FCM from tests.
        for target in (
            "chat.views._broadcast_new_message",
            "chat.views._broadcast_chat_notification",
            "chat.groups.broadcast_group_event",
        ):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create(self, **extra):
        body = {
            "name": "Gold",
            "member_ids": [str(self.follower.pk), str(self.followed.pk)],
            **extra,
        }
        self.client.force_authenticate(self.analyst)
        return self.client.post("/api/chat/groups/", body, format="json")

    def test_analyst_creates_group_with_followers_and_followed(self):
        res = self._create(only_admins_can_send=True)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["member_count"], 3)
        self.assertTrue(res.data["only_admins_can_send"])
        self.assertTrue(res.data["only_admins_can_edit_info"])
        self.assertEqual(res.data["my_role"], "admin")
        roles = {m["id"]: m["role"] for m in res.data["members"]}
        self.assertEqual(roles[str(self.analyst.pk)], "admin")
        self.assertEqual(roles[str(self.follower.pk)], "member")

    def test_trader_cannot_create_group(self):
        self.client.force_authenticate(self.follower)
        res = self.client.post(
            "/api/chat/groups/",
            {"name": "x", "member_ids": [str(self.analyst.pk)]},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_cannot_add_unrelated_or_malformed_ids(self):
        self.client.force_authenticate(self.analyst)
        res = self.client.post(
            "/api/chat/groups/",
            {"name": "x", "member_ids": [str(self.stranger.pk), "not-a-uuid"]},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(set(res.data["invalid_member_ids"]), {str(self.stranger.pk), "not-a-uuid"})
        self.assertFalse(Conversation.objects.filter(is_group=True).exists())

    def test_pending_follow_is_not_eligible(self):
        Follow.objects.filter(follower=self.follower).update(status=Follow.Status.PENDING)
        res = self._create()
        self.assertEqual(res.status_code, 400)

    def test_eligible_members_endpoint(self):
        self.client.force_authenticate(self.analyst)
        res = self.client.get("/api/chat/groups/eligible-members/")
        self.assertEqual(res.status_code, 200)
        rel = {r["id"]: r["relation"] for r in res.data["results"]}
        self.assertEqual(rel, {str(self.follower.pk): "follower", str(self.followed.pk): "following"})
        self.assertEqual({r["user_type"] for r in res.data["results"]}, {"trader"})
        res = self.client.get("/api/chat/groups/eligible-members/?search=fol")
        self.assertEqual(len(res.data["results"]), 1)

    def test_eligible_members_user_type_filter(self):
        coach = _user("coach", "analyst")
        _follow(coach, self.analyst)
        self.client.force_authenticate(self.analyst)
        url = "/api/chat/groups/eligible-members/"
        res = self.client.get(url + "?user_type=analyst")
        self.assertEqual([r["id"] for r in res.data["results"]], [str(coach.pk)])
        res = self.client.get(url + "?user_type=trader")
        self.assertEqual(len(res.data["results"]), 2)
        res = self.client.get(url + "?user_type=trader&search=fed")
        self.assertEqual(len(res.data["results"]), 1)
        self.assertEqual(self.client.get(url + "?user_type=admin").status_code, 400)

    def test_admin_only_messaging(self):
        gid = self._create(only_admins_can_send=True).data["id"]
        url = f"/api/chat/conversations/{gid}/messages/"
        self.client.force_authenticate(self.follower)
        self.assertEqual(self.client.post(url, {"content": "hi"}, format="json").status_code, 403)
        self.client.force_authenticate(self.analyst)
        self.assertEqual(self.client.post(url, {"content": "hi"}, format="json").status_code, 201)
        # Members can still read.
        self.client.force_authenticate(self.follower)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_open_messaging_and_toggle_setting(self):
        gid = self._create().data["id"]
        url = f"/api/chat/conversations/{gid}/messages/"
        self.client.force_authenticate(self.follower)
        self.assertEqual(self.client.post(url, {"content": "hi"}, format="json").status_code, 201)
        # Member cannot change settings; admin can, and it takes effect.
        res = self.client.patch(f"/api/chat/groups/{gid}/", {"only_admins_can_send": True}, format="json")
        self.assertEqual(res.status_code, 403)
        self.client.force_authenticate(self.analyst)
        res = self.client.patch(f"/api/chat/groups/{gid}/", {"only_admins_can_send": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["only_admins_can_send"])
        self.client.force_authenticate(self.follower)
        self.assertEqual(self.client.post(url, {"content": "again"}, format="json").status_code, 403)
        detail = self.client.get(f"/api/chat/groups/{gid}/")
        self.assertFalse(detail.data["can_send"])

    def test_edit_info_permission(self):
        gid = self._create().data["id"]
        self.client.force_authenticate(self.follower)
        res = self.client.patch(f"/api/chat/groups/{gid}/", {"name": "Hijack"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.client.force_authenticate(self.analyst)
        self.client.patch(f"/api/chat/groups/{gid}/", {"only_admins_can_edit_info": False}, format="json")
        self.client.force_authenticate(self.follower)
        res = self.client.patch(f"/api/chat/groups/{gid}/", {"name": " Renamed "}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["name"], "Renamed")

    def test_two_member_group_is_not_mistaken_for_direct_chat(self):
        self.client.force_authenticate(self.analyst)
        res = self.client.post(
            "/api/chat/groups/",
            {"name": "Duo", "member_ids": [str(self.follower.pk)]},
            format="json",
        )
        conv, created = Conversation.get_or_create_direct(self.analyst, self.follower)
        self.assertTrue(created)
        self.assertNotEqual(str(conv.pk), res.data["id"])

    def test_list_shows_group_with_per_member_unread_and_mark_read(self):
        gid = self._create().data["id"]
        ChatMessage.objects.create(conversation_id=gid, sender=self.analyst, content="one")
        ChatMessage.objects.create(conversation_id=gid, sender=self.analyst, content="two")
        self.client.force_authenticate(self.follower)
        row = self.client.get("/api/chat/conversations/").data["results"][0]
        self.assertTrue(row["is_group"])
        self.assertEqual(row["name"], "Gold")
        self.assertEqual(row["member_count"], 3)
        self.assertEqual(row["unread_count"], 2)
        self.client.post(f"/api/chat/conversations/{gid}/read/")
        self.assertEqual(self.client.get("/api/chat/conversations/").data["results"][0]["unread_count"], 0)
        # The follower's read must not mark the group read for another member.
        self.client.force_authenticate(self.followed)
        self.assertEqual(self.client.get("/api/chat/conversations/").data["results"][0]["unread_count"], 2)

    def test_one_to_one_list_shape_unchanged(self):
        conv, _ = Conversation.get_or_create_direct(self.analyst, self.follower)
        ChatMessage.objects.create(conversation=conv, sender=self.analyst, content="yo")
        self.client.force_authenticate(self.follower)
        row = self.client.get("/api/chat/conversations/").data["results"][0]
        self.assertFalse(row["is_group"])
        self.assertEqual(row["unread_count"], 1)
        self.assertEqual(len(row["participants"]), 2)

    def test_blocking_a_member_does_not_hide_or_lock_the_group(self):
        from Moderation.models import UserBlock

        gid = self._create().data["id"]
        UserBlock.objects.create(blocker=self.follower, blocked=self.followed)
        self.client.force_authenticate(self.follower)
        self.assertEqual(len(self.client.get("/api/chat/conversations/").data["results"]), 1)
        res = self.client.post(f"/api/chat/conversations/{gid}/messages/", {"content": "hi"}, format="json")
        self.assertEqual(res.status_code, 201)

    def test_add_remove_and_leave(self):
        gid = self._create().data["id"]
        _follow(self.stranger, self.analyst)
        self.client.force_authenticate(self.analyst)
        res = self.client.post(
            f"/api/chat/groups/{gid}/members/", {"member_ids": [str(self.stranger.pk)]}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["added"], res.data["member_count"]), (1, 4))
        self.assertEqual(
            self.client.delete(f"/api/chat/groups/{gid}/members/{self.stranger.pk}/").status_code, 204
        )
        self.assertEqual(
            self.client.delete(f"/api/chat/groups/{gid}/members/{self.analyst.pk}/").status_code, 400
        )
        # Removed user is locked out; a member can leave; the admin cannot.
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.client.get(f"/api/chat/groups/{gid}/").status_code, 403)
        self.client.force_authenticate(self.follower)
        self.assertEqual(
            self.client.delete(f"/api/chat/groups/{gid}/members/{self.followed.pk}/").status_code, 403
        )
        self.assertEqual(self.client.post(f"/api/chat/groups/{gid}/leave/").status_code, 200)
        self.client.force_authenticate(self.analyst)
        self.assertEqual(self.client.post(f"/api/chat/groups/{gid}/leave/").status_code, 400)

    def test_close_group_blocks_sending_and_hides_it(self):
        gid = self._create().data["id"]
        self.client.force_authenticate(self.follower)
        self.assertEqual(self.client.delete(f"/api/chat/groups/{gid}/").status_code, 403)
        self.client.force_authenticate(self.analyst)
        self.assertEqual(self.client.delete(f"/api/chat/groups/{gid}/").status_code, 204)
        self.assertEqual(self.client.get("/api/chat/conversations/").data["results"], [])
        res = self.client.post(f"/api/chat/conversations/{gid}/messages/", {"content": "x"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertTrue(Conversation.objects.filter(pk=gid).exists())

    def test_mute(self):
        gid = self._create().data["id"]
        self.client.force_authenticate(self.follower)
        res = self.client.post(f"/api/chat/groups/{gid}/mute/", {"muted": True}, format="json")
        self.assertEqual(res.data, {"muted": True})
        self.assertTrue(ConversationParticipant.objects.get(conversation_id=gid, user=self.follower).is_muted)
        self.assertTrue(self.client.get(f"/api/chat/groups/{gid}/").data["is_muted"])
