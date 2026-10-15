from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Admin configuration for custom User model
    """
    list_display = ('email', 'username', 'first_name', 'last_name', 'is_staff', 'is_verified', 'created_at')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'is_verified', 'created_at')
    search_fields = ('email', 'username', 'first_name', 'last_name')
    ordering = ('-created_at',)
    
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Additional Info', {
            'fields': (
                'phone_number',
                'profile_picture',
                'date_of_birth',
                'is_verified',
                'news_notify_ar',
                'news_notify_en',
                'news_notify_zh',
            )
        }),
    )
    
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Additional Info', {
            'fields': (
                'email',
                'phone_number',
                'profile_picture',
                'date_of_birth',
                'news_notify_ar',
                'news_notify_en',
                'news_notify_zh',
            )
        }),
    )


from .models import DeviceToken, PushDelivery  # noqa: E402


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ('user', 'platform', 'device_id', 'is_active', 'failure_count', 'last_error',
                    'last_seen_at', 'last_success_at')
    list_filter = ('platform', 'is_active')
    search_fields = ('user__email', 'device_id')
    raw_id_fields = ('user',)
    readonly_fields = ('token_hash', 'created_at', 'updated_at')


@admin.register(PushDelivery)
class PushDeliveryAdmin(admin.ModelAdmin):
    """Delivery log: filter by user (paste the user id) to see what reached which device."""
    list_display = ('created_at', 'push_type', 'status', 'platform', 'device_id', 'user_id', 'error_code',
                    'notification_id')
    list_filter = ('status', 'platform', 'push_type')
    search_fields = ('user_id', 'notification_id', 'device_id')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
