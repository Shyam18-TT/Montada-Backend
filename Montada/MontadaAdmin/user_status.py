"""
Account status used by the admin user lists (traders and analysts).

Each user has exactly one status, checked in this order:
    inactive  - account deleted (soft-deleted) by the user or an admin
    suspended - blocked by an admin (is_active=False)
    pending   - email not yet verified via OTP
    active    - everything else
"""

from django.db.models import Q

ACTIVE = "active"
SUSPENDED = "suspended"
INACTIVE = "inactive"
PENDING = "pending"

# Order shown in the admin filter dropdown.
USER_STATUS_CHOICES = (
    (ACTIVE, "Active"),
    (PENDING, "Pending"),
    (SUSPENDED, "Suspended"),
    (INACTIVE, "Inactive"),
)

STATUS_FILTERS = {
    INACTIVE: Q(is_soft_deleted=True),
    SUSPENDED: Q(is_soft_deleted=False, is_active=False),
    PENDING: Q(is_soft_deleted=False, is_active=True, is_verified=False),
    ACTIVE: Q(is_soft_deleted=False, is_active=True, is_verified=True),
}


def filter_users_by_status(qs, status_param):
    """Apply ?status=<value>; unknown or empty values leave the queryset unfiltered."""
    condition = STATUS_FILTERS.get((status_param or "").strip().lower())
    return qs.filter(condition) if condition is not None else qs


def user_status(user):
    if user.is_soft_deleted:
        return INACTIVE
    if not user.is_active:
        return SUSPENDED
    if not user.is_verified:
        return PENDING
    return ACTIVE
