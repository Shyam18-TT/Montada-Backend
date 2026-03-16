from rest_framework import serializers
from .models import TradingSignal, AssetClass, Instrument, Timeframe, AppliedSignal, PriceAlert


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
            'status', 'is_win', 'is_loss', 'is_neutral',
            'is_active', 'created_at', 'updated_at'
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

        # When closing a signal, require exactly one of is_win, is_loss, or is_neutral to be True
        final_status = attrs.get('status', getattr(self.instance, 'status', None))
        if final_status == TradingSignal.Status.CLOSED:
            final_is_win = attrs.get('is_win') if 'is_win' in attrs else getattr(self.instance, 'is_win', None)
            final_is_loss = attrs.get('is_loss') if 'is_loss' in attrs else getattr(self.instance, 'is_loss', None)
            final_is_neutral = attrs.get('is_neutral') if 'is_neutral' in attrs else getattr(self.instance, 'is_neutral', None)
            if sum([final_is_win is True, final_is_loss is True, final_is_neutral is True]) != 1:
                raise serializers.ValidationError({
                    'is_win': 'When closing a signal, set exactly one of is_win, is_loss, or is_neutral to true.'
                })
            # Normalize so the other two are False when saving
            if final_is_win is True:
                attrs['is_win'], attrs['is_loss'], attrs['is_neutral'] = True, False, False
            elif final_is_loss is True:
                attrs['is_win'], attrs['is_loss'], attrs['is_neutral'] = False, True, False
            else:
                attrs['is_win'], attrs['is_loss'], attrs['is_neutral'] = False, False, True

        return attrs


class ApplySignalSerializer(serializers.Serializer):
    """
    Serializer for trader to apply a signal (POST body).
    """
    signal = serializers.UUIDField(required=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


class AppliedSignalSerializer(serializers.ModelSerializer):
    """
    Serializer for AppliedSignal (list/detail response).
    """
    signal = TradingSignalSerializer(read_only=True)

    class Meta:
        model = AppliedSignal
        fields = ('id', 'signal', 'applied_at', 'note')
        read_only_fields = ('id', 'applied_at')


class PriceAlertCreateSerializer(serializers.ModelSerializer):
    """
    Create a price alert (POST).
    Either set target_price (fixed price) OR target_percentage + reference_price (e.g. 5% above 1.05).
    """
    instrument = serializers.PrimaryKeyRelatedField(
        queryset=Instrument.objects.filter(is_active=True),
        required=True,
    )

    class Meta:
        model = PriceAlert
        fields = ('instrument', 'target_price', 'target_percentage', 'reference_price', 'condition', 'label')
        read_only_fields = ()

    def validate_target_price(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError("Target price must be greater than 0.")
        return value

    def validate_target_percentage(self, value):
        if value is not None and (value <= 0 or value >= 100):
            raise serializers.ValidationError("Target percentage must be between 0 and 100 (exclusive).")
        return value

    def validate_reference_price(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError("Reference price must be greater than 0.")
        return value

    def validate(self, attrs):
        has_price = attrs.get('target_price') is not None
        has_pct = attrs.get('target_percentage') is not None
        has_ref = attrs.get('reference_price') is not None
        if has_price and (has_pct or has_ref):
            raise serializers.ValidationError(
                "Use either target_price OR (target_percentage + reference_price), not both."
            )
        if not has_price and not (has_pct and has_ref):
            raise serializers.ValidationError(
                "Provide either target_price or both target_percentage and reference_price."
            )
        return attrs

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class PriceAlertSerializer(serializers.ModelSerializer):
    """List/detail price alert (read)."""
    instrument_symbol = serializers.CharField(source='instrument.symbol', read_only=True)
    instrument_name = serializers.CharField(source='instrument.name', read_only=True)
    effective_target_price = serializers.SerializerMethodField()

    class Meta:
        model = PriceAlert
        fields = (
            'id', 'instrument', 'instrument_symbol', 'instrument_name',
            'target_price', 'target_percentage', 'reference_price', 'condition', 'label',
            'effective_target_price',
            'is_triggered', 'triggered_at', 'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'instrument', 'instrument_symbol', 'instrument_name',
            'target_price', 'target_percentage', 'reference_price', 'condition', 'label',
            'effective_target_price',
            'is_triggered', 'triggered_at', 'created_at', 'updated_at',
        )

    def get_effective_target_price(self, obj):
        eff = obj.get_effective_target_price()
        return str(eff) if eff is not None else None

