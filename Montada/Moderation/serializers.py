from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import ModerationReport, UserBlock


User = get_user_model()


class ModerationReportSerializer(serializers.ModelSerializer):
    reported_user_id = serializers.CharField(
    write_only=True,
    max_length=64,
    required=False,
    allow_blank=True,
    )

    class Meta:
        model = ModerationReport
        fields = (
            "id",
            "reporter",
            "content_type",
            "reported_user_id",
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
            "reporter",
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

        # Skip validation if the field is omitted
        if not value:
            return value

        try:
            if not User.objects.filter(pk=value).exists():
                raise serializers.ValidationError("Reported user not found.")
        except (DjangoValidationError, TypeError, ValueError):
            raise serializers.ValidationError("Reported user ID is invalid.")

        return value

    def create(self, validated_data):
        reported_user = None

        reported_user_id = validated_data.pop("reported_user_id", None)

        if reported_user_id:
            try:
                reported_user = User.objects.get(pk=reported_user_id)
            except (User.DoesNotExist, DjangoValidationError, TypeError, ValueError):
                raise serializers.ValidationError({
                    "reported_user_id": "Reported user not found."
                })

        return ModerationReport.objects.create(
            reporter=self.context["request"].user,
            reported_user=reported_user,
            **validated_data,
        )

    def validate(self, attrs):
        content_type = attrs.get("content_type")
        reported_user_id = attrs.get("reported_user_id")

        if content_type == "user":
            if not reported_user_id:
                raise serializers.ValidationError({
                    "reported_user_id": "This field is required when content_type is 'user'."
                })

        return attrs
    
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
