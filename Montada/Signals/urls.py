from django.urls import path
from .views import (
    CreateTradingSignalView,
    AssetClassListView,
    InstrumentListView,
    AssetClassWithInstrumentsView
)

app_name = 'Signals'

urlpatterns = [
    path('create/', CreateTradingSignalView.as_view(), name='create_signal'),
    path('asset-classes/', AssetClassListView.as_view(), name='asset_classes'),
    path('instruments/', InstrumentListView.as_view(), name='instruments'),
    path('assets-instruments/', AssetClassWithInstrumentsView.as_view(), name='assets_instruments'),
]

