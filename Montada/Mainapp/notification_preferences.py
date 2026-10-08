"""
Per-user push notification preferences.

Every push the app sends belongs to one category below. For each category a user
chooses a mode:

    sound  - push plays a sound
    silent - push is delivered without sound
    off    - no push (default: users opt in from settings)

In-app notifications are not affected: every user always gets them.

How it is enforced
------------------
firebase.send_push_to_users() maps the payload's data["type"] to a category
(category_for_push) and splits recipients by mode, so every sender is covered.

Only changed categories are stored (NotificationPreference rows); a missing row means
DEFAULT_MODE. Queries filter by category, never by a list of user ids, so broadcasts to
every user stay a single small query (and avoid SQL Server's 2100-parameter limit).
Account notices (MANDATORY_ADMIN_BROADCAST_CATEGORIES) ignore preferences and always go out.
"""
from collections import OrderedDict

SOUND = "sound"
SILENT = "silent"
OFF = "off"
MODES = (SOUND, SILENT, OFF)
DEFAULT_MODE = OFF


class Category:
    MESSAGES = "MESSAGES"
    TRADE_IDEAS = "TRADE_IDEAS"
    MY_SIGNAL_ACTIVITY = "MY_SIGNAL_ACTIVITY"
    PRICE_ALERTS = "PRICE_ALERTS"
    SYMBOL_MOVES = "SYMBOL_MOVES"
    ECONOMIC_EVENTS = "ECONOMIC_EVENTS"
    ECONOMIC_REMINDERS = "ECONOMIC_REMINDERS"
    NEWS = "NEWS"
    ANNOUNCEMENTS = "ANNOUNCEMENTS"


# Display order of the settings screen. "analyst_only" categories are hidden from traders.
CATEGORIES = OrderedDict([
    (Category.MESSAGES, {
        "label": "Messages",
        "description": "New chat messages.",
        "push_types": ("chat_message",),
    }),
    (Category.TRADE_IDEAS, {
        "label": "Trade ideas",
        "description": "Signals published, updated or closed by analysts you follow.",
        "push_types": ("signal_published", "signal_closed", "signal_alert", "new_signal", "signal_update"),
    }),
    (Category.MY_SIGNAL_ACTIVITY, {
        "label": "Activity on my signals",
        "description": "Traders applying your signals and your signals hitting take profit or stop loss.",
        "push_types": ("signal_applied", "price_alert"),
        "analyst_only": True,
    }),
    (Category.PRICE_ALERTS, {
        "label": "Price alerts",
        "description": "Price alerts you created.",
        "push_types": ("user_price_alert",),
    }),
    (Category.SYMBOL_MOVES, {
        "label": "Market moves",
        "description": "Large daily percentage changes on symbols.",
        "push_types": ("signal_change_threshold",),
    }),
    (Category.ECONOMIC_EVENTS, {
        "label": "Economic events",
        "description": "Upcoming and released economic calendar events.",
        "push_types": ("economic_event", "economic_global_reminder"),
    }),
    (Category.ECONOMIC_REMINDERS, {
        "label": "Economic event reminders",
        "description": "Reminders you set on economic calendar events.",
        "push_types": ("economic_reminder",),
    }),
    (Category.NEWS, {
        "label": "News",
        "description": "Live market news.",
        "push_types": ("news_update",),
    }),
    (Category.ANNOUNCEMENTS, {
        "label": "Announcements",
        "description": "Announcements and offers from Montada.",
        "push_types": ("admin_broadcast",),
    }),
])

_CATEGORY_BY_PUSH_TYPE = {
    push_type: key
    for key, meta in CATEGORIES.items()
    for push_type in meta["push_types"]
}

# Admin broadcast categories users cannot opt out of (account / system notices).
MANDATORY_ADMIN_BROADCAST_CATEGORIES = frozenset({"system_alert", "subscription_reminder"})


def is_valid_category(category):
    return category in CATEGORIES


def categories_for_user(user):
    """Category keys shown to this user (analyst-only ones are hidden from traders)."""
    is_analyst = getattr(user, "user_type", None) == "analyst"
    return [
        key for key, meta in CATEGORIES.items()
        if is_analyst or not meta.get("analyst_only")
    ]


def category_for_push(data):
    """Preference category of an FCM payload, or None when preferences do not apply."""
    data = data or {}
    push_type = str(data.get("type") or "").strip().lower()
    if push_type == "admin_broadcast":
        admin_category = str(data.get("category") or "").strip().lower()
        if admin_category in MANDATORY_ADMIN_BROADCAST_CATEGORIES:
            return None
    return _CATEGORY_BY_PUSH_TYPE.get(push_type)


def get_user_modes(user):
    """{category: mode} for every category, defaults filled in."""
    from Mainapp.models import NotificationPreference

    modes = {key: DEFAULT_MODE for key in CATEGORIES}
    for category, mode in NotificationPreference.objects.filter(user=user).values_list("category", "mode"):
        if category in modes and mode in MODES:
            modes[category] = mode
    return modes


def _mode_by_user_id(category):
    """{user_id: mode} for users whose mode for `category` differs from DEFAULT_MODE."""
    from Mainapp.models import NotificationPreference

    return dict(
        NotificationPreference.objects.filter(category=category, mode__in=MODES)
        .exclude(mode=DEFAULT_MODE)
        .values_list("user_id", "mode")
    )


def _user_id(user):
    return getattr(user, "id", user)


def enabled_users_q(category):
    """Q for a User queryset matching users who receive `category` (no id lists in SQL)."""
    from django.db.models import Q
    from Mainapp.models import NotificationPreference

    if DEFAULT_MODE == OFF:
        opted_in = NotificationPreference.objects.filter(category=category, mode__in=(SOUND, SILENT))
        return Q(id__in=opted_in.values("user_id"))
    opted_out = NotificationPreference.objects.filter(category=category, mode=OFF)
    return ~Q(id__in=opted_out.values("user_id"))


def split_by_sound(users, category):
    """(with_sound, without_sound) for `category`; users who have it off are dropped."""
    users = list(users or [])
    if not users or not category:
        return users, []
    mode_by_user_id = _mode_by_user_id(category)
    with_sound, without_sound = [], []
    for user in users:
        mode = mode_by_user_id.get(_user_id(user), DEFAULT_MODE)
        if mode == SOUND:
            with_sound.append(user)
        elif mode == SILENT:
            without_sound.append(user)
    return with_sound, without_sound


def save_user_modes(user, modes):
    """Store {category: mode}. Rows equal to the default are deleted to keep the table small."""
    from django.db import transaction
    from Mainapp.models import NotificationPreference

    with transaction.atomic():
        for category, mode in modes.items():
            if mode == DEFAULT_MODE:
                NotificationPreference.objects.filter(user=user, category=category).delete()
            else:
                NotificationPreference.objects.update_or_create(
                    user=user, category=category, defaults={"mode": mode}
                )
