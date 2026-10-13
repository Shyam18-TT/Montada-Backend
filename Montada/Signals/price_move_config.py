"""
Admin-controlled config for the symbol %-move notifications sent by
poll_signal_change_notifications ("EURUSD up 0.5%").

Admins edit PriceMoveNotificationSettings (one row of global defaults) and
PriceMoveNotificationRule rows (per asset class or per symbol: own thresholds, or muted).

Caching: the worker keeps a compiled PriceMoveConfig in memory and re-reads the DB only
when the Redis version stamp changes. The stamp is rewritten after every committed change
to those tables, and to AssetClass/Instrument because they decide which asset-class rule
a symbol falls under. So a poll costs one Redis GET, and an admin change costs one reload
(3 queries). The worker also reloads every CONFIG_MAX_AGE_SECONDS in case a change slipped
past the stamp (queryset.update(), bulk_create, raw SQL, Redis flush).
"""
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)

VERSION_CACHE_KEY = "signals:price-move-config:version"
CONFIG_MAX_AGE_SECONDS = 300
# After a failed DB load, retry this soon instead of waiting CONFIG_MAX_AGE_SECONDS.
CONFIG_RETRY_SECONDS = 60

# Behaviour before admin control existed: seed data for the tables and the fallback used
# when they cannot be read (e.g. code deployed before `migrate`).
DEFAULT_THRESHOLD_PERCENT = Decimal("0.5")
DEFAULT_RESET_PERCENT = Decimal("0.3")
DEFAULT_SHARE_THRESHOLD_PERCENT = Decimal("5.0")
DEFAULT_SHARE_STEP_PERCENT = Decimal("5.0")
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_MAX_NOTIFICATION_BATCHES = 200

# Validation bounds shared by the admin API and the worker.
MIN_PERCENT = Decimal("0.05")
MAX_PERCENT = Decimal("100")
MIN_POLL_INTERVAL_SECONDS = 5
MAX_POLL_INTERVAL_SECONDS = 3600
MAX_COOLDOWN_SECONDS = 86400
MAX_NOTIFICATION_BATCHES = 1000

# "FX 2" (Gulf/EGP pegs) and "Forex Minors" never trigger change notifications (seeded as muted).
EXCLUDED_FX2_SYMBOLS = ("USDSAR", "USDEGP", "USDAED", "USDQAR", "USDKWD")
EXCLUDED_FOREX_MINOR_SYMBOLS = (
    "AUDNOK", "AUDSGD", "CADSGD", "CHFSGD", "EURCZK", "EURHUF", "EURNOK", "EURPLN", "EURSEK",
    "EURSGD", "EURZAR", "GBPMXN", "GBPNOK", "GBPSEK", "GBPSGD", "NOKJPY", "NOKSEK", "SGDJPY",
    "USDCNH", "USDCZK", "USDHKD", "USDHUF", "USDMXN", "USDNOK", "USDPLN", "USDSEK", "USDSGD",
    "USDTHB", "USDZAR", "ZARJPY",
)
# Crypto pairs that never trigger change notifications (seeded as muted).
EXCLUDED_CRYPTO_SYMBOLS = (
    "LTCUSD", "LNKUSD", "DOTUSD", "XLMUSD", "DSHUSD", "AAVUSD", "BATUSD", "ETCUSD", "FILUSD",
    "IOTUSD", "NEOUSD", "SUSUSD", "XTZUSD", "TETUSD", "TRXUSD", "UNIUSD", "ZECUSD", "ETHUSD",
)
# Asset classes whose instruments use the 5% share threshold.
SHARE_ASSET_CLASS_NAMES = frozenset({
    "share", "shares", "stock", "stocks", "equity", "equities", "mena shares", "menashares",
    "crypto", "cryptos", "cryptocurrency", "cryptocurrencies",
})
# Crypto symbols as named by the live-quote feed. Signals.utils has no matching category.
CRYPTO_SYMBOLS = frozenset({
    "ADAUSD", "BCHUSD", "BNBUSD", "BTCUSD", "DOGEUSD", "DOGUSD", "DOTUSD", "ETHUSD",
    "LNKUSD", "LTCUSD", "SOLUSD", "XLMUSD", "XRPUSD",
})
# Agricultural commodity CFDs use the 5% threshold; a rule on the base name matches every
# contract month (WHEAT_Z26, WHEAT_H27, ...).
AGRICULTURAL_BASE_SYMBOLS = frozenset({
    "WHEAT", "COCOA", "COFFEE", "CORN", "SOYBEAN", "COTTON", "SUGAR",
})
# Symbols where the live-quote feed uses a different name than Instrument.symbol in the DB.
# Mirrors SYMBOL_ALIASES_DB in run_price_alerts.py.
SYMBOL_ALIASES = {
    "GOLDUSD": ["GOLD", "XAUUSD"],
    "SILVERUSD": ["SILVER", "XAGUSD"],
    "XAUUSD": ["GOLD"],
    "XAGUSD": ["SILVER"],
    "S&P500": ["US500", "SP500"],
    "SP500": ["US500"],
    "DOWJONES": ["US30", "DOW"],
    "NIFTY50": ["NIFTY", "NIFTY50"],
    "DOGEUSD": ["DOGE", "DOGUSD"],
}

# Dated futures contracts in the feed: base + "_" or "." + month code + 2-digit year.
_FUTURES_CONTRACT_RE = re.compile(r"^(.+?)[._][FGHJKMNQUVXZ]\d{2}$")
# Broker variants of a symbol: AUDNOK.p, AUDNOK., AUDNOKp. A bare trailing "P" is only
# stripped from forex-length names so e.g. SHOP never matches a rule on SHO.
_MIN_LENGTH_FOR_P_SUFFIX = 7


def normalize_symbol(symbol):
    return str(symbol or "").replace("/", "").replace(" ", "").upper()


def futures_base_symbol(symbol):
    """Base symbol of a dated futures contract (US100_Z26 / US100.Z26 -> US100), else None."""
    match = _FUTURES_CONTRACT_RE.match(symbol)
    return match.group(1) if match else None


def symbol_alias_candidates(symbol):
    """All symbols equivalent to `symbol` per SYMBOL_ALIASES, including itself."""
    candidates = {symbol}
    candidates.update(SYMBOL_ALIASES.get(symbol, []))
    for key, aliases in SYMBOL_ALIASES.items():
        if symbol == key or symbol in aliases:
            candidates.add(key)
            candidates.update(aliases)
    return candidates


def _broker_variant_base(symbol):
    if symbol.endswith(".P"):
        return symbol[:-2]
    if symbol.endswith("."):
        return symbol[:-1]
    if symbol.endswith("P") and len(symbol) >= _MIN_LENGTH_FOR_P_SUFFIX:
        return symbol[:-1]
    return None


def symbol_match_candidates(symbol):
    """
    Names a rule may be stored under for feed symbol `symbol`, most specific first:
    the symbol itself, its aliases, its futures base, then its broker-variant base.
    """
    ordered = []

    def add(names):
        for name in names:
            if name and name not in ordered:
                ordered.append(name)

    add([symbol])
    add(sorted(symbol_alias_candidates(symbol) - {symbol}))
    for base in (futures_base_symbol(symbol), _broker_variant_base(symbol)):
        if base:
            add([base])
            add(sorted(symbol_alias_candidates(base) - {base}))
    return ordered


def format_percent(value):
    normalized = Decimal(value).normalize()
    # format(..., "f") avoids scientific notation: Decimal("10").normalize() is 1E+1.
    if normalized == normalized.to_integral():
        return format(normalized.to_integral(), "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def _clean_percent(value, *, allow_zero=False):
    """Decimal within the allowed range, else None (bad rows written outside the admin API)."""
    if value is None:
        return None
    try:
        value = Decimal(str(value))
    except Exception:
        return None
    low = Decimal("0") if allow_zero else MIN_PERCENT
    if value < low or value > MAX_PERCENT:
        return None
    return value


def _clean_int(value, low, high):
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value < low or value > high:
        return None
    return value


@dataclass(frozen=True)
class EffectiveRule:
    """Settings that apply to one symbol after layering default -> asset class -> symbol."""
    enabled: bool
    threshold: Decimal
    step: Decimal
    reset: Decimal
    cooldown_seconds: int
    source: str
    warnings: tuple = ()

    @property
    def signature(self):
        """
        Identifies the level grid a symbol's notification state was built with. When it
        changes (threshold/step edited, muted, unmuted) the worker re-baselines the symbol
        from its current % change instead of sending the levels the new grid "missed".
        """
        return "%s|%s|%s" % (
            "on" if self.enabled else "off",
            format_percent(self.threshold),
            format_percent(self.step),
        )

    def levels(self, count=5):
        return [self.threshold + self.step * Decimal(i) for i in range(count)]


_RULE_VALUE_FIELDS = ("threshold", "step", "reset", "cooldown_seconds")


@dataclass
class PriceMoveConfig:
    enabled: bool = True
    threshold: Decimal = DEFAULT_THRESHOLD_PERCENT
    step: Decimal = None
    reset: Decimal = DEFAULT_RESET_PERCENT
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    max_notification_batches: int = DEFAULT_MAX_NOTIFICATION_BATCHES
    # normalized symbol -> rule dict; asset class id (str) -> rule dict
    symbol_rules: dict = field(default_factory=dict)
    asset_class_rules: dict = field(default_factory=dict)
    # normalized Instrument.symbol -> {asset class id (str), ...}
    instrument_asset_classes: dict = field(default_factory=dict)
    source: str = "db"
    version: object = None

    def resolve(self, symbol, asset_class_ids=()):
        """
        Effective settings for feed symbol `symbol` (normalized). `asset_class_ids` adds
        asset classes known from elsewhere (e.g. open signals on the symbol).

        Precedence: symbol rule > asset-class rule > global defaults, field by field.
        A symbol in several asset classes with rules takes the strictest values: muted if
        any of them is muted, else the largest threshold/step/reset/cooldown.
        """
        candidates = symbol_match_candidates(symbol)
        class_ids = {str(i) for i in asset_class_ids if i}
        for candidate in candidates:
            class_ids.update(self.instrument_asset_classes.get(candidate, ()))
        class_rules = [self.asset_class_rules[i] for i in sorted(class_ids) if i in self.asset_class_rules]
        symbol_rule = next((self.symbol_rules[c] for c in candidates if c in self.symbol_rules), None)

        enabled = None
        values = dict.fromkeys(_RULE_VALUE_FIELDS)
        sources = []
        if class_rules:
            flags = [rule["enabled"] for rule in class_rules if rule["enabled"] is not None]
            if False in flags:
                enabled = False
            elif True in flags:
                enabled = True
            for name in _RULE_VALUE_FIELDS:
                found = [rule[name] for rule in class_rules if rule[name] is not None]
                if found:
                    values[name] = max(found)
            sources.extend(rule["label"] for rule in class_rules)
        if symbol_rule:
            if symbol_rule["enabled"] is not None:
                enabled = symbol_rule["enabled"]
            for name in _RULE_VALUE_FIELDS:
                if symbol_rule[name] is not None:
                    values[name] = symbol_rule[name]
            sources.append(symbol_rule["label"])

        threshold = values["threshold"] or self.threshold
        if values["step"] is not None:
            step = values["step"]
        elif values["threshold"] is not None:
            # A rule that only sets a threshold gets evenly spaced levels (5, 10, 15, ...).
            step = values["threshold"]
        else:
            step = self.step or self.threshold
        reset = values["reset"] if values["reset"] is not None else self.reset
        warnings = []
        if reset >= threshold:
            clamped = threshold * Decimal("0.6")
            warnings.append(
                "reset %s%% is not below threshold %s%%; using %s%%."
                % (format_percent(reset), format_percent(threshold), format_percent(clamped))
            )
            reset = clamped
        cooldown = values["cooldown_seconds"] if values["cooldown_seconds"] is not None else self.cooldown_seconds

        if not self.enabled:
            final_enabled = False
            sources.append("global switch off")
        else:
            final_enabled = True if enabled is None else enabled
        return EffectiveRule(
            enabled=final_enabled,
            threshold=threshold,
            step=step,
            reset=reset,
            cooldown_seconds=cooldown,
            source=" > ".join(["default"] + sources),
            warnings=tuple(warnings),
        )


def _rule_dict(*, enabled, threshold, step, reset, cooldown_seconds, label):
    return {
        "enabled": enabled,
        "threshold": _clean_percent(threshold),
        "step": _clean_percent(step),
        "reset": _clean_percent(reset, allow_zero=True),
        "cooldown_seconds": _clean_int(cooldown_seconds, 0, MAX_COOLDOWN_SECONDS),
        "label": label,
    }


def load_config_from_db(version=None):
    """Compile the admin tables into a PriceMoveConfig (3 queries)."""
    from Signals.models import Instrument, PriceMoveNotificationRule, PriceMoveNotificationSettings

    row = PriceMoveNotificationSettings.load()
    config = PriceMoveConfig(
        enabled=bool(row.is_enabled),
        threshold=_clean_percent(row.default_threshold_percent) or DEFAULT_THRESHOLD_PERCENT,
        step=_clean_percent(row.default_step_percent),
        reset=_clean_percent(row.reset_percent, allow_zero=True),
        cooldown_seconds=_clean_int(row.cooldown_seconds, 0, MAX_COOLDOWN_SECONDS),
        poll_interval_seconds=_clean_int(
            row.poll_interval_seconds, MIN_POLL_INTERVAL_SECONDS, MAX_POLL_INTERVAL_SECONDS
        ),
        max_notification_batches=_clean_int(row.max_notification_batches, 1, MAX_NOTIFICATION_BATCHES),
        source="db",
        version=version,
    )
    if config.reset is None:
        config.reset = DEFAULT_RESET_PERCENT
    if config.cooldown_seconds is None:
        config.cooldown_seconds = DEFAULT_COOLDOWN_SECONDS
    if config.poll_interval_seconds is None:
        config.poll_interval_seconds = DEFAULT_POLL_INTERVAL_SECONDS
    if config.max_notification_batches is None:
        config.max_notification_batches = DEFAULT_MAX_NOTIFICATION_BATCHES

    for rule in PriceMoveNotificationRule.objects.select_related("asset_class"):
        data = _rule_dict(
            enabled=rule.is_enabled,
            threshold=rule.threshold_percent,
            step=rule.step_percent,
            reset=rule.reset_percent,
            cooldown_seconds=rule.cooldown_seconds,
            label="%s:%s" % (rule.scope, rule.target_label),
        )
        if rule.scope == PriceMoveNotificationRule.Scope.SYMBOL:
            symbol = normalize_symbol(rule.symbol)
            if symbol:
                config.symbol_rules[symbol] = data
        elif rule.scope == PriceMoveNotificationRule.Scope.ASSET_CLASS and rule.asset_class_id:
            config.asset_class_rules[str(rule.asset_class_id)] = data

    for symbol, asset_class_id in Instrument.objects.values_list("symbol", "asset_class_id"):
        normalized = normalize_symbol(symbol)
        if normalized and asset_class_id:
            config.instrument_asset_classes.setdefault(normalized, set()).add(str(asset_class_id))
    return config


def legacy_seed_rules(asset_classes=(), instruments=()):
    """
    Rules that reproduce the behaviour from before admin control.

    asset_classes: iterable of (id, name); instruments: iterable of (symbol, asset_class_id).
    Share/crypto asset classes get an asset-class rule; listed share, crypto and agricultural
    symbols not already covered by one get a symbol rule; FX 2, forex minors and the excluded
    crypto pairs are muted (a muted symbol gets only the mute rule, never a threshold rule).
    """
    from Signals.utils import MARKET_DATA_SYMBOLS_BY_CATEGORY

    rules = []
    share_class_ids = set()
    for asset_class_id, name in asset_classes:
        if str(name or "").strip().lower() in SHARE_ASSET_CLASS_NAMES:
            share_class_ids.add(asset_class_id)
            rules.append({
                "scope": "asset_class",
                "asset_class_id": asset_class_id,
                "symbol": None,
                "is_enabled": None,
                "threshold_percent": DEFAULT_SHARE_THRESHOLD_PERCENT,
                "step_percent": DEFAULT_SHARE_STEP_PERCENT,
                "note": "Seeded: shares/crypto use 5%, then every 5%.",
            })

    covered = {
        normalize_symbol(symbol)
        for symbol, asset_class_id in instruments
        if asset_class_id in share_class_ids
    }
    share_symbols = {
        normalize_symbol(symbol)
        for category in ("shares", "menashares")
        for symbol in MARKET_DATA_SYMBOLS_BY_CATEGORY.get(category, [])
    }
    share_symbols.update(CRYPTO_SYMBOLS)
    muted_symbols = set(EXCLUDED_FX2_SYMBOLS) | set(EXCLUDED_FOREX_MINOR_SYMBOLS) | set(EXCLUDED_CRYPTO_SYMBOLS)
    for symbol in sorted(share_symbols - covered - muted_symbols):
        rules.append({
            "scope": "symbol",
            "asset_class_id": None,
            "symbol": symbol,
            "is_enabled": None,
            "threshold_percent": DEFAULT_SHARE_THRESHOLD_PERCENT,
            "step_percent": DEFAULT_SHARE_STEP_PERCENT,
            "note": "Seeded: share/crypto symbol uses 5%, then every 5%.",
        })
    for symbol in sorted(AGRICULTURAL_BASE_SYMBOLS - covered - muted_symbols):
        rules.append({
            "scope": "symbol",
            "asset_class_id": None,
            "symbol": symbol,
            "is_enabled": None,
            "threshold_percent": DEFAULT_SHARE_THRESHOLD_PERCENT,
            "step_percent": DEFAULT_SHARE_STEP_PERCENT,
            "note": "Seeded: agricultural CFD (all contract months) uses 5%, then every 5%.",
        })
    for symbol in sorted(muted_symbols):
        rules.append({
            "scope": "symbol",
            "asset_class_id": None,
            "symbol": symbol,
            "is_enabled": False,
            "threshold_percent": None,
            "step_percent": None,
            "note": (
                "Seeded: excluded crypto pair, never notified."
                if symbol in EXCLUDED_CRYPTO_SYMBOLS
                else "Seeded: FX 2 / forex minor, never notified."
            ),
        })
    return rules


def seed_price_move_config(settings_model, rule_model, asset_class_model, instrument_model):
    """
    Create the settings row and any missing legacy rules. Existing rows are never changed,
    so admin edits survive a re-run. Takes model classes so migrations can pass historical ones.
    """
    settings_model.objects.get_or_create(pk=1)
    existing_classes = set(
        rule_model.objects.filter(scope="asset_class").values_list("asset_class_id", flat=True)
    )
    existing_symbols = set(rule_model.objects.filter(scope="symbol").values_list("symbol", flat=True))
    to_create = []
    for row in legacy_seed_rules(
        asset_class_model.objects.values_list("id", "name"),
        instrument_model.objects.values_list("symbol", "asset_class_id"),
    ):
        if row["scope"] == "asset_class" and row["asset_class_id"] in existing_classes:
            continue
        if row["scope"] == "symbol" and row["symbol"] in existing_symbols:
            continue
        to_create.append(rule_model(**row))
    rule_model.objects.bulk_create(to_create)
    return len(to_create)


def build_fallback_config(
    *,
    threshold=DEFAULT_THRESHOLD_PERCENT,
    reset=DEFAULT_RESET_PERCENT,
    cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
    poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
    max_notification_batches=DEFAULT_MAX_NOTIFICATION_BATCHES,
):
    """Legacy behaviour without the DB (symbol rules only; asset classes need the DB)."""
    config = PriceMoveConfig(
        threshold=threshold,
        reset=reset,
        cooldown_seconds=cooldown_seconds,
        poll_interval_seconds=poll_interval_seconds,
        max_notification_batches=max_notification_batches,
        source="fallback",
    )
    for row in legacy_seed_rules():
        config.symbol_rules[row["symbol"]] = _rule_dict(
            enabled=row["is_enabled"],
            threshold=row["threshold_percent"],
            step=row["step_percent"],
            reset=None,
            cooldown_seconds=None,
            label="symbol:%s" % row["symbol"],
        )
    return config


def bump_config_version(**kwargs):
    """
    Tell workers to reload, once the current transaction commits. Bumping inside the
    transaction would let a worker reload the old rows and keep them until the next change.
    Usable directly as a post_save/post_delete receiver.
    """
    def _bump():
        try:
            cache.set(VERSION_CACHE_KEY, uuid.uuid4().hex, timeout=None)
        except Exception:
            logger.exception(
                "Could not bump price move config version; workers reload within %ss.",
                CONFIG_MAX_AGE_SECONDS,
            )

    transaction.on_commit(_bump)


def connect_invalidation_signals():
    from django.db.models.signals import post_delete, post_save
    from Signals.models import (
        AssetClass,
        Instrument,
        PriceMoveNotificationRule,
        PriceMoveNotificationSettings,
    )

    for model in (PriceMoveNotificationSettings, PriceMoveNotificationRule, AssetClass, Instrument):
        for signal, name in ((post_save, "save"), (post_delete, "delete")):
            signal.connect(
                bump_config_version,
                sender=model,
                dispatch_uid="price_move_config_bump:%s:%s" % (model.__name__, name),
            )


class PriceMoveConfigProvider:
    """
    In-memory config for a worker process. get() costs one Redis GET; the DB is read only
    when the version stamp changed or the copy is older than max_age_seconds. If the DB
    cannot be read, the last good config is kept (or the fallback, before any load).
    """

    def __init__(self, fallback_factory, max_age_seconds=CONFIG_MAX_AGE_SECONDS):
        self._fallback_factory = fallback_factory
        self._max_age_seconds = max_age_seconds
        self._config = None
        self._version = None
        self._next_reload_at = 0.0

    def _read_version(self):
        try:
            return cache.get(VERSION_CACHE_KEY), True
        except Exception:
            logger.warning("Could not read price move config version from cache.", exc_info=True)
            return None, False

    def get(self):
        if self._config is None or time.monotonic() >= self._next_reload_at:
            self._reload()
        else:
            version, ok = self._read_version()
            if ok and version != self._version:
                self._reload(version)
        return self._config

    def _reload(self, version=None):
        if version is None:
            version, _ = self._read_version()
        # The version is read before the rows: a change committed in between bumps it
        # again, so the next get() reloads once more instead of keeping old rows.
        try:
            config = load_config_from_db(version)
        except Exception:
            logger.exception("Could not load price move config from DB; keeping %s config.",
                             "last good" if self._config is not None else "fallback")
            if self._config is None:
                self._config = self._fallback_factory()
            self._version = version
            self._next_reload_at = time.monotonic() + CONFIG_RETRY_SECONDS
            return
        self._config = config
        self._version = version
        self._next_reload_at = time.monotonic() + self._max_age_seconds
