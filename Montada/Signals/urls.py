from django.urls import path
from .views import (
    CreateTradingSignalView,
    AssetClassListView,
    InstrumentListView,
    AssetClassWithInstrumentsView,
    AnalystSignalListView,
    AnalystSignalUpdateView,
    AnalystSignalSoftDeleteView,
    TimeframeListView
)

app_name = 'Signals'

urlpatterns = [
    path('create/', CreateTradingSignalView.as_view(), name='create_signal'),
    path('my-signals/', AnalystSignalListView.as_view(), name='analyst_signals_list'),
    path('edit-my-signals/<str:pk>/', AnalystSignalUpdateView.as_view(), name='analyst_signal_update'),
    path('delete-my-signals/<str:pk>/', AnalystSignalSoftDeleteView.as_view(), name='analyst_signal_delete'),
    path('asset-classes/', AssetClassListView.as_view(), name='asset_classes'),
    path('instruments/', InstrumentListView.as_view(), name='instruments'),
    path('timeframes/', TimeframeListView.as_view(), name='timeframes'),
    path('assets-instruments/', AssetClassWithInstrumentsView.as_view(), name='assets_instruments'),
]

