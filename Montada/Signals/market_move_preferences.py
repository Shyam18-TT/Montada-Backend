"""
Per-user control over which instruments send market-move alerts ("EURUSD up 0.5%").

Thresholds, steps and the percentage calculation stay admin-controlled
(price_move_config.py); users only choose *which* instruments notify them.

Resolution order for one user and one symbol:
    1. Global switch   - Mainapp NotificationPreference SYMBOL_MOVES == off -> nothing.
    2. Symbol choice   - MarketMovePreference(scope=symbol), used whatever the category says.
    3. Category choice - MarketMovePreference(scope=category).
    4. System default  - enabled (the admin rules in price_move_config still decide whether
       the symbol alerts at all; a symbol the admin muted never alerts for anyone).

Only explicit choices are stored; resetting deletes the row, which restores inheritance.
"""
from Signals.price_move_config import (
    AGRICULTURAL_BASE_SYMBOLS,
    CRYPTO_SYMBOLS,
    normalize_symbol,
    symbol_match_candidates,
)
from Signals.utils import MARKET_DATA_SYMBOLS_BY_CATEGORY

CATEGORY_LABELS = {
    "forex": "Forex",
    "metals": "Metals",
    "energy": "Energy",
    "indices": "Indices",
    "agriculture": "Agriculture",
    "crypto": "Crypto",
    "mena_shares": "MENA Shares",
    "share_cfds": "Share CFDs",
}
CATEGORY_KEYS = tuple(CATEGORY_LABELS)
SYSTEM_DEFAULT_ENABLED = True

# utils.MARKET_DATA_SYMBOLS_BY_CATEGORY key -> category key.
_UTILS_CATEGORY = {
    "forex": "forex",
    "metals": "metals",
    "energy": "energy",
    "indices": "indices",
    "commodity": "agriculture",
    "crypto": "crypto",
    "menashares": "mena_shares",
    "shares": "share_cfds",
}
# AssetClass.name (lower case) -> category key, for instruments the static lists do not know.
_ASSET_CLASS_CATEGORY = {
    "forex": "forex",
    "metals": "metals",
    "metal": "metals",
    "energy": "energy",
    "indices": "indices",
    "index": "indices",
    "commodity": "agriculture",
    "commodities": "agriculture",
    "agriculture": "agriculture",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "mena shares": "mena_shares",
    "menashares": "mena_shares",
    "shares": "share_cfds",
    "share cfds": "share_cfds",
    "stocks": "share_cfds",
}


def _build_static_map():
    mapping = {}
    for utils_key, symbols in MARKET_DATA_SYMBOLS_BY_CATEGORY.items():
        category = _UTILS_CATEGORY.get(utils_key)
        if category:
            for symbol in symbols:
                mapping.setdefault(normalize_symbol(symbol), category)
    for symbol in CRYPTO_SYMBOLS:
        mapping.setdefault(normalize_symbol(symbol), "crypto")
    for symbol in AGRICULTURAL_BASE_SYMBOLS:
        mapping.setdefault(normalize_symbol(symbol), "agriculture")
    return mapping


_STATIC_CATEGORY_BY_SYMBOL = _build_static_map()


def category_for_symbol(symbol, asset_class_names=()):
    """Category key of a feed/instrument symbol, or None when it fits none of the eight."""
    for candidate in symbol_match_candidates(normalize_symbol(symbol)):
        category = _STATIC_CATEGORY_BY_SYMBOL.get(candidate)
        if category:
            return category
    for name in asset_class_names or ():
        category = _ASSET_CLASS_CATEGORY.get(str(name or "").strip().lower())
        if category:
            return category
    return None


def _asset_class_names_for(symbol):
    from Signals.models import Instrument

    candidates = symbol_match_candidates(normalize_symbol(symbol))
    return list(
        Instrument.objects.filter(symbol__in=candidates).values_list("asset_class__name", flat=True)
    )


def resolve_enabled(*, symbol_choice, category_choice):
    """
    (enabled, source) from explicit choices, each True / False / None (= not set).
    The global switch is checked separately (global_enabled).
    """
    if symbol_choice is not None:
        return symbol_choice, "symbol"
    if category_choice is not None:
        return category_choice, "category"
    return SYSTEM_DEFAULT_ENABLED, "default"


def global_enabled(user):
    from Mainapp.notification_preferences import Category, OFF, get_user_modes

    return get_user_modes(user)[Category.SYMBOL_MOVES] != OFF


def user_choices(user):
    """({normalized symbol: bool}, {category key: bool}) explicitly chosen by `user`."""
    from Signals.models import MarketMovePreference

    symbols, categories = {}, {}
    for scope, key, enabled in MarketMovePreference.objects.filter(user=user).values_list(
        "scope", "key", "enabled"
    ):
        (symbols if scope == MarketMovePreference.Scope.SYMBOL else categories)[key] = enabled
    return symbols, categories


def symbol_choice_for(symbol_choices, symbol):
    """Explicit choice for a feed symbol, trying aliases/futures/broker variants too."""
    for candidate in symbol_match_candidates(normalize_symbol(symbol)):
        if candidate in symbol_choices:
            return symbol_choices[candidate]
    return None


def opted_out_user_ids(symbol, asset_class_names=None):
    """
    IDs of users who must not get a market-move alert for feed `symbol` (symbol/category
    choices only; the global switch is applied by send_push_to_users). Queried by scope/key,
    never by a list of user ids, so it is safe for broadcasts to every user.
    """
    from Signals.models import MarketMovePreference

    candidates = symbol_match_candidates(normalize_symbol(symbol))
    rank = {name: index for index, name in enumerate(candidates)}

    # The most specific candidate wins per user.
    chosen = {}
    for user_id, key, enabled in MarketMovePreference.objects.filter(
        scope=MarketMovePreference.Scope.SYMBOL, key__in=candidates
    ).values_list("user_id", "key", "enabled"):
        best = chosen.get(user_id)
        if best is None or rank[key] < best[0]:
            chosen[user_id] = (rank[key], enabled)

    if asset_class_names is None:
        asset_class_names = _asset_class_names_for(symbol)
    category = category_for_symbol(symbol, asset_class_names)
    category_off = set()
    if category:
        category_off = set(
            MarketMovePreference.objects.filter(
                scope=MarketMovePreference.Scope.CATEGORY, key=category, enabled=False
            ).values_list("user_id", flat=True)
        )

    excluded = {user_id for user_id in category_off if user_id not in chosen}
    excluded.update(user_id for user_id, (_, enabled) in chosen.items() if not enabled)
    return excluded


def set_category_choices(user, choices):
    """{category key: True/False/None}; None resets to the system default."""
    from django.db import transaction
    from Signals.models import MarketMovePreference

    with transaction.atomic():
        for key, enabled in choices.items():
            if enabled is None:
                MarketMovePreference.objects.filter(
                    user=user, scope=MarketMovePreference.Scope.CATEGORY, key=key
                ).delete()
            else:
                MarketMovePreference.objects.update_or_create(
                    user=user, scope=MarketMovePreference.Scope.CATEGORY, key=key,
                    defaults={"enabled": enabled},
                )


def set_symbol_choice(user, symbol, enabled):
    """enabled True/False stores a choice; None resets to category inheritance."""
    from Signals.models import MarketMovePreference

    key = normalize_symbol(symbol)
    if enabled is None:
        MarketMovePreference.objects.filter(
            user=user, scope=MarketMovePreference.Scope.SYMBOL, key=key
        ).delete()
    else:
        MarketMovePreference.objects.update_or_create(
            user=user, scope=MarketMovePreference.Scope.SYMBOL, key=key,
            defaults={"enabled": enabled},
        )
