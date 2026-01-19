from rest_framework import serializers
from .models import TradingSignal


class TradingSignalSerializer(serializers.ModelSerializer):
    """
    Serializer for TradingSignal model
    """
    analyst_name = serializers.CharField(source='analyst.name', read_only=True)
    analyst_email = serializers.EmailField(source='analyst.email', read_only=True)
    
    class Meta:
        model = TradingSignal
        fields = (
            'id', 'analyst', 'analyst_name', 'analyst_email',
            'asset_class', 'instrument', 'direction', 'entry_price',
            'stop_loss', 'take_profit', 'timeframe', 'confidence_level',
            'analyst_note', 'is_active', 'created_at', 'updated_at'
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
        
        return attrs

