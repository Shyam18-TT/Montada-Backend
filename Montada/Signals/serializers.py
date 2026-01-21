from rest_framework import serializers
from .models import TradingSignal, AssetClass, Instrument, Timeframe


class AssetClassSerializer(serializers.ModelSerializer):
    """
    Serializer for AssetClass model
    """
    class Meta:
        model = AssetClass
        fields = ('id', 'name', 'description', 'is_active', 'created_at')
        read_only_fields = ('id', 'created_at')


class InstrumentSerializer(serializers.ModelSerializer):
    """
    Serializer for Instrument model
    """
    asset_class_name = serializers.CharField(source='asset_class.name', read_only=True)
    
    class Meta:
        model = Instrument
        fields = ('id', 'asset_class', 'asset_class_name', 'symbol', 'name', 'is_active', 'created_at')
        read_only_fields = ('id', 'created_at')


class TimeframeSerializer(serializers.ModelSerializer):
    """
    Serializer for Timeframe model
    """
    class Meta:
        model = Timeframe
        fields = ('id', 'code', 'name', 'description', 'is_active', 'created_at')
        read_only_fields = ('id', 'created_at')


class TimeframeSimpleSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for Timeframe model
    Returns only id, code, and name
    """
    class Meta:
        model = Timeframe
        fields = ('id', 'code', 'name')
        read_only_fields = ('id',)


class InstrumentNestedSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for instruments when nested within asset classes
    Returns only id and name for active instruments
    Uses symbol if name is null
    """
    name = serializers.SerializerMethodField()
    
    class Meta:
        model = Instrument
        fields = ('id', 'name')
        read_only_fields = ('id',)
    
    def get_name(self, obj):
        """
        Return name if available, otherwise return symbol
        """
        return obj.name if obj.name else obj.symbol


class AssetClassWithInstrumentsSerializer(serializers.ModelSerializer):
    """
    Serializer for AssetClass with nested instruments
    Returns only id and name for active asset classes and their active instruments
    """
    instruments = serializers.SerializerMethodField()
    
    class Meta:
        model = AssetClass
        fields = ('id', 'name', 'instruments')
        read_only_fields = ('id',)
    
    def get_instruments(self, obj):
        """
        Get only active instruments for this asset class (id and name only)
        """
        active_instruments = obj.instruments.filter(is_active=True)
        return InstrumentNestedSerializer(active_instruments, many=True).data


class TradingSignalSerializer(serializers.ModelSerializer):
    """
    Serializer for TradingSignal model
    """
    analyst_name = serializers.CharField(source='analyst.name', read_only=True)
    analyst_email = serializers.EmailField(source='analyst.email', read_only=True)
    asset_class_name = serializers.CharField(source='asset_class.name', read_only=True)
    instrument_symbol = serializers.CharField(source='instrument.symbol', read_only=True)
    instrument_name = serializers.CharField(source='instrument.name', read_only=True)
    timeframe_code = serializers.CharField(source='timeframe.code', read_only=True)
    timeframe_name = serializers.CharField(source='timeframe.name', read_only=True)
    
    # Accept IDs from frontend
    asset_class = serializers.PrimaryKeyRelatedField(
        queryset=AssetClass.objects.filter(is_active=True),
        required=True
    )
    instrument = serializers.PrimaryKeyRelatedField(
        queryset=Instrument.objects.filter(is_active=True),
        required=True
    )
    timeframe = serializers.PrimaryKeyRelatedField(
        queryset=Timeframe.objects.filter(is_active=True),
        required=True
    )
    
    class Meta:
        model = TradingSignal
        fields = (
            'id', 'analyst', 'analyst_name', 'analyst_email',
            'asset_class', 'asset_class_name',
            'instrument', 'instrument_symbol', 'instrument_name',
            'direction', 'entry_price', 'stop_loss', 'take_profit',
            'timeframe', 'timeframe_code', 'timeframe_name',
            'confidence_level', 'analyst_note',
            'status', 'is_active', 'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'analyst', 'created_at', 'updated_at')
    
    def validate_confidence_level(self, value):
        """Validate confidence level is between 0 and 100"""
        if value < 0 or value > 100:
            raise serializers.ValidationError(
                "Confidence level must be between 0 and 100."
            )
        return value
    
    def validate(self, attrs):
        """Additional validation"""
        # Ensure entry_price, stop_loss, and take_profit are positive
        if attrs.get('entry_price') and attrs['entry_price'] <= 0:
            raise serializers.ValidationError({
                'entry_price': 'Entry price must be greater than 0.'
            })
        
        if attrs.get('stop_loss') and attrs['stop_loss'] <= 0:
            raise serializers.ValidationError({
                'stop_loss': 'Stop loss must be greater than 0.'
            })
        
        if attrs.get('take_profit') and attrs['take_profit'] <= 0:
            raise serializers.ValidationError({
                'take_profit': 'Take profit must be greater than 0.'
            })
        
        # Validate that instrument belongs to the selected asset_class
        asset_class = attrs.get('asset_class')
        instrument = attrs.get('instrument')
        
        if asset_class and instrument:
            if instrument.asset_class != asset_class:
                raise serializers.ValidationError({
                    'instrument': f'Instrument "{instrument.symbol}" does not belong to asset class "{asset_class.name}".'
                })
        
        return attrs

