from django.contrib import admin
from .models import (
    Subscription,
    AnalystContentPlan,
    UserAnalystPlanSubscription,
)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan_type', 'status', 'is_trial', 'start_date', 'end_date', 'days_remaining_display', 'is_active_display')
    list_filter = ('plan_type', 'status', 'is_trial')
    search_fields = ('user__email', 'user__username')
    readonly_fields = ('created_at', 'updated_at', 'days_remaining_display', 'is_active_display')
    
    fieldsets = (
        ('User Information', {
            'fields': ('user',)
        }),
        ('Subscription Details', {
            'fields': ('plan_type', 'status', 'is_trial', 'start_date', 'end_date')
        }),
        ('Status Information', {
            'fields': ('is_active_display', 'days_remaining_display')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )
    
    def days_remaining_display(self, obj):
        """Display days remaining"""
        return obj.days_remaining()
    days_remaining_display.short_description = 'Days Remaining'
    
    def is_active_display(self, obj):
        """Display if subscription is active"""
        return obj.is_active()
    is_active_display.short_description = 'Is Active'
    is_active_display.boolean = True


@admin.register(AnalystContentPlan)
class AnalystContentPlanAdmin(admin.ModelAdmin):
    list_display = ("title", "analyst", "scope", "price", "currency", "billing_period", "is_active", "created_at")
    list_filter = ("scope", "billing_period", "is_active")
    search_fields = ("title", "analyst__email", "description")
    readonly_fields = ("created_at", "updated_at")


@admin.register(UserAnalystPlanSubscription)
class UserAnalystPlanSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("subscriber", "plan", "status", "start_date", "end_date", "created_at")
    list_filter = ("status",)
    search_fields = ("subscriber__email", "plan__title")
    readonly_fields = ("created_at", "updated_at")
