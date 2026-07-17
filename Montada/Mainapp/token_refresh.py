from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .blocked import AccountBlocked, is_account_blocked
from .models import User


class BlockAwareTokenRefreshSerializer(TokenRefreshSerializer):
    """Prevent a blocked account from receiving a replacement access token."""

    def validate(self, attrs):
        refresh = RefreshToken(attrs["refresh"])
        user_id = refresh.get(api_settings.USER_ID_CLAIM)
        user = User.objects.filter(pk=user_id).first() if user_id else None
        if user and is_account_blocked(user):
            raise AccountBlocked()
        return super().validate(attrs)


class BlockAwareTokenRefreshView(TokenRefreshView):
    serializer_class = BlockAwareTokenRefreshSerializer
