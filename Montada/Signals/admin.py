from django.contrib import admin
from .models import (
    TradingSignal, AssetClass, Instrument, Timeframe, PriceAlert,
    PriceMoveNotificationRule, PriceMoveNotificationSettings,
)


@admin.register(AssetClass)
class AssetClassAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'description', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at',)
    ordering = ('name',)


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'symbol', 'name', 'asset_class', 'is_active', 'created_at')
    list_filter = ('asset_class', 'is_active', 'created_at')
    search_fields = ('symbol', 'name', 'asset_class__name')
    readonly_fields = ('created_at',)
    ordering = ('asset_class__name', 'symbol')


@admin.register(Timeframe)
class TimeframeAdmin(admin.ModelAdmin):
    list_display = ('id', 'code', 'name', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('code', 'name')
    readonly_fields = ('created_at',)
    ordering = ('code',)


@admin.register(TradingSignal)
class TradingSignalAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'analyst', 'instrument', 'asset_class', 'direction',
        'entry_price', 'stop_loss', 'take_profit', 'timeframe',
        'confidence_level', 'status', 'is_active', 'deleted_at', 'created_at'
    )
    list_filter = ('asset_class', 'direction', 'timeframe', 'status', 'is_active', 'deleted_at', 'created_at')
    search_fields = ('instrument__symbol', 'instrument__name', 'analyst__email', 'analyst__name', 'analyst_note')
    readonly_fields = ('created_at', 'updated_at', 'deleted_at')
    ordering = ('-created_at',)
    
    fieldsets = (
        ('Analyst Information', {
            'fields': ('analyst',)
        }),
        ('Signal Details', {
            'fields': (
                'asset_class', 'instrument', 'direction', 'timeframe',
                'entry_price', 'stop_loss', 'take_profit', 'confidence_level'
            )
        }),
        ('Status Information', {
            'fields': ('status', 'is_active', 'deleted_at')
        }),
        ('Additional Information', {
            'fields': ('analyst_note',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(PriceAlert)
class PriceAlertAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'instrument', 'target_price', 'target_percentage', 'reference_price', 'condition', 'label', 'is_triggered', 'triggered_at', 'created_at')
    list_filter = ('condition', 'is_triggered', 'created_at')
    search_fields = ('user__email', 'instrument__symbol', 'label')
    readonly_fields = ('created_at', 'updated_at', 'triggered_at')
    ordering = ('-created_at',)


@admin.register(PriceMoveNotificationSettings)
class PriceMoveNotificationSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'is_enabled', 'default_threshold_percent', 'default_step_percent', 'reset_percent',
        'cooldown_seconds', 'poll_interval_seconds', 'max_notification_batches', 'updated_at',
    )
    readonly_fields = ('updated_at', 'updated_by')

    def has_add_permission(self, request):
        return not PriceMoveNotificationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(PriceMoveNotificationRule)
class PriceMoveNotificationRuleAdmin(admin.ModelAdmin):
    list_display = (
        'scope', 'asset_class', 'symbol', 'is_enabled', 'threshold_percent', 'step_percent',
        'reset_percent', 'cooldown_seconds', 'updated_at',
    )
    list_filter = ('scope', 'is_enabled', 'asset_class')
    search_fields = ('symbol', 'asset_class__name', 'note')
    readonly_fields = ('created_at', 'updated_at', 'updated_by')

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
