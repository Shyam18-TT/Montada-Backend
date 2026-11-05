"""
User API for choosing which instruments send market-move alerts.

    GET    /api/signals/market-move-preferences/                 global switch + category choices
    PUT    /api/signals/market-move-preferences/categories/      set / reset categories
    GET    /api/signals/market-move-preferences/symbols/         search instruments + their choice
    PUT    /api/signals/market-move-preferences/symbols/<symbol>/    enable / disable one symbol
    DELETE /api/signals/market-move-preferences/symbols/<symbol>/    reset to the category choice

Thresholds are not configurable here; they stay admin-managed (price_move_config.py).
The global on/off switch is the existing "Market moves" category of
/api/auth/notification-preferences/ (key SYMBOL_MOVES).
"""
import logging

from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from Mainapp.audit import audit

from . import market_move_preferences as prefs
from .models import Instrument
from .price_move_config import normalize_symbol

logger = logging.getLogger(__name__)


class _Pagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class _CategoriesSerializer(serializers.Serializer):
    categories = serializers.DictField(
        child=serializers.BooleanField(allow_null=True),
        allow_empty=False,
        help_text="{category: true | false | null}. null resets to the system default.",
    )

    def validate_categories(self, value):
        unknown = sorted(set(value) - set(prefs.CATEGORY_KEYS))
        if unknown:
            raise serializers.ValidationError(
                "Unknown categories: %s. Valid: %s." % (", ".join(unknown), ", ".join(prefs.CATEGORY_KEYS))
            )
        return value


class _SymbolSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(allow_null=True)


def _category_rows(category_choices):
    rows = []
    for key in prefs.CATEGORY_KEYS:
        choice = category_choices.get(key)
        enabled, source = prefs.resolve_enabled(symbol_choice=None, category_choice=choice)
        rows.append({
            "key": key,
            "label": prefs.CATEGORY_LABELS[key],
            "preference": choice,
            "enabled": enabled,
            "source": source,
        })
    return rows


def _admin_enabled_lookup():
    """normalized symbol -> False for symbols the admin muted (they alert for nobody)."""
    try:
        from .price_move_config import load_config_from_db

        config = load_config_from_db()
    except Exception:
        logger.exception("Could not load price move config for the preference list.")
        return lambda symbol, asset_class_id: True

    def lookup(symbol, asset_class_id):
        return config.resolve(symbol, {asset_class_id} if asset_class_id else ()).enabled

    return lookup


def _symbol_row(instrument, symbol_choices, category_choices, admin_enabled):
    symbol = normalize_symbol(instrument.symbol)
    category = prefs.category_for_symbol(symbol, [instrument.asset_class.name])
    choice = symbol_choices.get(symbol)
    category_choice = category_choices.get(category) if category else None
    enabled, source = prefs.resolve_enabled(symbol_choice=choice, category_choice=category_choice)
    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "instrument_id": str(instrument.pk),
        "category": category,
        "category_label": prefs.CATEGORY_LABELS.get(category),
        "preference": choice,
        "enabled": enabled,
        "source": source,
        # False when an admin muted the symbol: no user is alerted whatever they choose.
        "alerts_available": admin_enabled(symbol, instrument.asset_class_id),
    }


def _find_instrument(symbol):
    wanted = normalize_symbol(symbol)
    for instrument in Instrument.objects.filter(is_active=True).select_related("asset_class"):
        if normalize_symbol(instrument.symbol) == wanted:
            return instrument
    return None


class MarketMovePreferencesView(APIView):
    """GET: the global switch and the eight category choices."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, category_choices = prefs.user_choices(request.user)
        return Response({
            "global": {
                "enabled": prefs.global_enabled(request.user),
                "notification_category": "SYMBOL_MOVES",
            },
            "categories": _category_rows(category_choices),
        })


class MarketMoveCategoriesView(APIView):
    """PUT/PATCH {"categories": {"forex": true, "energy": false, "metals": null}}; null resets."""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        serializer = _CategoriesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        choices = serializer.validated_data["categories"]
        prefs.set_category_choices(request.user, choices)
        audit("user.market_move_categories_updated", request=request, user=request.user.pk,
              categories=",".join(sorted(choices)))
        _, category_choices = prefs.user_choices(request.user)
        return Response({
            "global": {
                "enabled": prefs.global_enabled(request.user),
                "notification_category": "SYMBOL_MOVES",
            },
            "categories": _category_rows(category_choices),
        })

    def patch(self, request):
        return self.put(request)


class MarketMoveSymbolsView(APIView):
    """
    GET: instruments with the caller's choice for each.
    Query: ?search=<text> (symbol or name), ?category=<key>, ?preference=set|unset|enabled|disabled,
    ?page, ?page_size.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        category = (request.query_params.get("category") or "").strip().lower()
        if category and category not in prefs.CATEGORY_KEYS:
            return Response(
                {"error": "category must be one of: %s." % ", ".join(prefs.CATEGORY_KEYS)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        preference = (request.query_params.get("preference") or "").strip().lower()
        if preference and preference not in ("set", "unset", "enabled", "disabled"):
            return Response(
                {"error": "preference must be one of: set, unset, enabled, disabled."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = Instrument.objects.filter(is_active=True).select_related("asset_class")
        search = (request.query_params.get("search") or "").strip()
        if search:
            from django.db.models import Q

            qs = qs.filter(Q(symbol__icontains=search) | Q(name__icontains=search))
        qs = qs.order_by("symbol")

        symbol_choices, category_choices = prefs.user_choices(request.user)
        admin_enabled = _admin_enabled_lookup()
        rows = [_symbol_row(i, symbol_choices, category_choices, admin_enabled) for i in qs]
        if category:
            rows = [r for r in rows if r["category"] == category]
        if preference == "set":
            rows = [r for r in rows if r["preference"] is not None]
        elif preference == "unset":
            rows = [r for r in rows if r["preference"] is None]
        elif preference == "enabled":
            rows = [r for r in rows if r["enabled"]]
        elif preference == "disabled":
            rows = [r for r in rows if not r["enabled"]]

        paginator = _Pagination()
        page = paginator.paginate_queryset(rows, request)
        return paginator.get_paginated_response(page if page is not None else rows)


class MarketMoveSymbolDetailView(APIView):
    """
    PUT    {"enabled": true | false}: use this choice for the symbol, whatever its category says.
           {"enabled": null} resets, same as DELETE.
    DELETE reset: the symbol inherits its category choice again.
    """
    permission_classes = [IsAuthenticated]

    def _row(self, request, instrument):
        symbol_choices, category_choices = prefs.user_choices(request.user)
        return _symbol_row(instrument, symbol_choices, category_choices, _admin_enabled_lookup())

    def _instrument(self, symbol):
        instrument = _find_instrument(symbol)
        if instrument is None:
            from django.http import Http404

            raise Http404("Instrument not found.")
        return instrument

    def get(self, request, symbol):
        return Response(self._row(request, self._instrument(symbol)))

    def put(self, request, symbol):
        instrument = self._instrument(symbol)
        serializer = _SymbolSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enabled = serializer.validated_data["enabled"]
        prefs.set_symbol_choice(request.user, instrument.symbol, enabled)
        audit("user.market_move_symbol_updated", request=request, user=request.user.pk,
              symbol=normalize_symbol(instrument.symbol),
              enabled="reset" if enabled is None else str(enabled).lower())
        return Response(self._row(request, instrument))

    def patch(self, request, symbol):
        return self.put(request, symbol)

    def delete(self, request, symbol):
        instrument = self._instrument(symbol)
        prefs.set_symbol_choice(request.user, instrument.symbol, None)
        audit("user.market_move_symbol_updated", request=request, user=request.user.pk,
              symbol=normalize_symbol(instrument.symbol), enabled="reset")
        return Response(self._row(request, instrument))
