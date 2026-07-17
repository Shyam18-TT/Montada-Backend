from rest_framework_simplejwt.authentication import JWTAuthentication

from .blocked import AccountBlocked, is_account_blocked


class BlockAwareJWTAuthentication(JWTAuthentication):
    """Reject access tokens belonging to an account blocked by an admin."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if is_account_blocked(user):
            raise AccountBlocked()
        return user
