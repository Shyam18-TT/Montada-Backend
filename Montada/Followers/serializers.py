from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Follow, Mute, AnalystReview

User = get_user_model()


class UserMinimalSerializer(serializers.ModelSerializer):
    """Minimal user for followers/following lists to avoid circular import."""
    class Meta:
        model = User
        fields = ("id", "username", "email", "name", "profile_picture", "user_type", "created_at")


class FollowerListItemSerializer(serializers.ModelSerializer):
    """Serializes a Follow instance as its follower (User) for paginated list. Used with Follow queryset."""
    class Meta:
        model = Follow
        fields = ()

    def to_representation(self, instance):
        data = UserMinimalSerializer(instance.follower).data
        data["applied_signals_count"] = getattr(instance, "applied_signals_count", 0)
        return data


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


class AnalystReviewSubmitSerializer(serializers.Serializer):
    """Body for POST /followers/reviews/ — create or update review for an analyst."""

    analyst_id = serializers.UUIDField(help_text="UUID of the analyst being reviewed")
    rating = serializers.IntegerField(min_value=1, max_value=5, help_text="1–5 stars")
    title = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
    )
    review_text = serializers.CharField(
        max_length=4000,
        required=False,
        allow_blank=True,
        default="",
    )
    is_public = serializers.BooleanField(default=True, required=False)


class AnalystReviewReadSerializer(serializers.ModelSerializer):
    """Saved review row returned to the client."""

    analyst_id = serializers.UUIDField(read_only=True)
    reviewer_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = AnalystReview
        fields = (
            "id",
            "analyst_id",
            "reviewer_id",
            "rating",
            "title",
            "review_text",
            "is_public",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields
