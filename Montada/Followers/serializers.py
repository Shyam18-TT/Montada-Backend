from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Follow, Mute

User = get_user_model()


class UserMinimalSerializer(serializers.ModelSerializer):
    """Minimal user for followers/following lists to avoid circular import."""
    class Meta:
        model = User
        fields = ("id", "username", "email", "name", "profile_picture", "user_type")


class FollowSerializer(serializers.ModelSerializer):
    follower_detail = UserMinimalSerializer(source="follower", read_only=True)
    followed_detail = UserMinimalSerializer(source="followed", read_only=True)

    class Meta:
        model = Follow
        fields = (
            "id", "follower", "followed", "status", "is_active",
            "requested_at", "accepted_at", "rejected_at", "unfollowed_at",
            "follower_detail", "followed_detail",
        )
        read_only_fields = (
            "id", "follower", "followed", "status", "is_active",
            "requested_at", "accepted_at", "rejected_at", "unfollowed_at",
            "follower_detail", "followed_detail",
        )


class FollowRequestSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=True, help_text="ID of user to send follow request to")


class FollowActionSerializer(serializers.Serializer):
    follow_id = serializers.UUIDField(required=True, help_text="ID of the Follow record")


class MuteSerializer(serializers.ModelSerializer):
    muter_detail = UserMinimalSerializer(source="muter", read_only=True)
    muted_detail = UserMinimalSerializer(source="muted", read_only=True)

    class Meta:
        model = Mute
        fields = ("id", "muter", "muted", "muted_at", "muter_detail", "muted_detail")
        read_only_fields = ("id", "muter", "muted", "muted_at", "muter_detail", "muted_detail")


class MuteActionSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=True, help_text="ID of user to mute/unmute")
