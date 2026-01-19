from django.contrib import admin
from .models import TradingSignal, AssetClass, Instrument


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


@admin.register(TradingSignal)
class TradingSignalAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'analyst', 'instrument', 'asset_class', 'direction',
        'entry_price', 'stop_loss', 'take_profit', 'timeframe',
        'confidence_level', 'is_active', 'created_at'
    )
    list_filter = ('asset_class', 'direction', 'timeframe', 'is_active', 'created_at')
    search_fields = ('instrument__symbol', 'instrument__name', 'analyst__email', 'analyst__name', 'analyst_note')
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
