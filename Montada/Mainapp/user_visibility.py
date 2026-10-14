"""
Which accounts may appear in user lists shown to app clients (followers, following,
analysts, chat, ...).

Suspended users (is_active=False) and deleted users (is_soft_deleted=True) are hidden
from other users. Admin lists do not use this: they show every account with its status
(see MontadaAdmin.user_status).
"""

from django.db.models import Q


def listable_users_q(prefix=""):
    """
    Q matching users that may be shown to clients.

    ``prefix`` is the relation path to the user, e.g. ``"follower__"`` when filtering
    ``Follow`` rows by the follower. Leave empty when filtering ``User`` directly.
    """
    return Q(**{f"{prefix}is_active": True, f"{prefix}is_soft_deleted": False})


def is_listable_user(user):
    return bool(user.is_active) and not user.is_soft_deleted
