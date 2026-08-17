from rest_framework.exceptions import APIException


class AccountBlocked(APIException):
    status_code = 451
    default_detail = "This account has been blocked."
    default_code = "ACCOUNT_BLOCKED"


def is_account_blocked(user):
    """Return whether an admin (staff) has banned this account.

    Peer-to-peer blocks (one regular user blocking another) do not lock the
    blocked user out of the platform - they only restrict interaction with
    that specific blocker. Only a block issued by a staff/admin account is
    treated as a platform-wide ban.
    """
    if not user or not getattr(user, "pk", None):
        return False

    from Moderation.models import UserBlock

    return UserBlock.objects.filter(
        blocked_id=user.pk,
        blocker__is_staff=True,
    ).exists()
