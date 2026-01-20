from django.urls import path
from .views import (
    CreateTradingSignalView,
    AssetClassListView,
    InstrumentListView,
    AssetClassWithInstrumentsView,
    AnalystSignalListView,
    AnalystSignalUpdateView
)

app_name = 'Signals'

urlpatterns = [
    path('create/', CreateTradingSignalView.as_view(), name='create_signal'),
    path('my-signals/', AnalystSignalListView.as_view(), name='analyst_signals_list'),
    path('edit-my-signals/<int:pk>/', AnalystSignalUpdateView.as_view(), name='analyst_signal_update'),
    path('asset-classes/', AssetClassListView.as_view(), name='asset_classes'),
    path('instruments/', InstrumentListView.as_view(), name='instruments'),
    path('assets-instruments/', AssetClassWithInstrumentsView.as_view(), name='assets_instruments'),
]

