from rest_framework import serializers

from Mainapp.models import ActivityLog


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
