from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import ModerationReport, UserBlock


User = get_user_model()


class ModerationReportSerializer(serializers.ModelSerializer):
    reported_user_id = serializers.CharField(write_only=True, max_length=64)

    class Meta:
        model = ModerationReport
        fields = (
            "id",
            "content_type",
            "reported_user_id",
            "reported_user_id_raw",
            "reported_user",
            "content_id",
            "content_excerpt",
            "reason",
            "details",
            "reported_at",
            "platform",
            "status",
            "created_at",
        )
        read_only_fields = (
            "id",
            "reported_user_id_raw",
            "reported_user",
            "status",
            "created_at",
        )
        extra_kwargs = {
            "content_type": {"required": True, "max_length": 40},
            "content_id": {"required": False, "allow_blank": True, "max_length": 64},
            "content_excerpt": {"required": False, "allow_blank": True, "max_length": 500},
            "reason": {"required": True, "max_length": 64},
            "details": {"required": False, "allow_blank": True, "max_length": 300},
            "reported_at": {"required": True},
            "platform": {"required": True, "allow_blank": False, "max_length": 16},
        }

    def validate_reported_user_id(self, value):
        value = str(value or "").strip()
        if not value:
            raise serializers.ValidationError("reported_user_id is required.")
        return value

    def create(self, validated_data):
        reported_user_id = validated_data.pop("reported_user_id")
        reported_user = None
        try:
            reported_user = User.objects.filter(pk=reported_user_id).first()
        except (DjangoValidationError, TypeError, ValueError):
            reported_user = None
        return ModerationReport.objects.create(
            reporter=self.context["request"].user,
            reported_user=reported_user,
            reported_user_id_raw=reported_user_id,
            **validated_data,
        )


class UserBlockSerializer(serializers.ModelSerializer):
    blocked_user_id = serializers.CharField(write_only=True, max_length=64)

    class Meta:
        model = UserBlock
        fields = ("id", "blocked_user_id", "blocked", "created_at")
        read_only_fields = ("id", "blocked", "created_at")

    def validate_blocked_user_id(self, value):
        value = str(value or "").strip()
        if not value:
            raise serializers.ValidationError("blocked_user_id is required.")
        return value
