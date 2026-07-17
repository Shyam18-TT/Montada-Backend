from rest_framework.exceptions import APIException


class AccountBlocked(APIException):
    status_code = 403
    default_detail = "This account has been blocked."
    default_code = "ACCOUNT_BLOCKED"


def is_account_blocked(user):
    """Return whether an admin has created a UserBlock for this account."""
    if not user or not getattr(user, "pk", None):
        return False

    from Moderation.models import UserBlock

    return UserBlock.objects.filter(blocked_id=user.pk).exists()
