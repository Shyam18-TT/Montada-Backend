from django.contrib import admin
from .models import TradingSignal


@admin.register(TradingSignal)
class TradingSignalAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'analyst', 'instrument', 'asset_class', 'direction',
        'entry_price', 'stop_loss', 'take_profit', 'timeframe',
        'confidence_level', 'is_active', 'created_at'
    )
    list_filter = ('asset_class', 'direction', 'timeframe', 'is_active', 'created_at')
    search_fields = ('instrument', 'analyst__email', 'analyst__name', 'analyst_note')
    readonly_fields = ('created_at', 'updated_at')
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
        ('Additional Information', {
            'fields': ('analyst_note', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )
