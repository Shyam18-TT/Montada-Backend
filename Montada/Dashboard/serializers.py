from rest_framework import serializers

from Mainapp.models import ActivityLog, UserNotification


class UserNotificationSerializer(serializers.ModelSerializer):
    """Serializer for UserNotification (list/detail)."""

    class Meta:
        model = UserNotification
        fields = (
            "id",
            "title",
            "message",
            "notification_type",
            "is_read",
            "redirect_url",
            "created_at",
            "read_at",
        )
        read_only_fields = fields


class ActivityLogSerializer(serializers.ModelSerializer):
    """Serializer for ActivityLog list (analyst recent activities)."""

    class Meta:
        model = ActivityLog
        fields = (
            'id',
            'type',
            'title',
            'subtitle',
            'icon',
            'entity_type',
            'entity_id',
            'metadata',
            'created_at',
        )
        read_only_fields = fields
