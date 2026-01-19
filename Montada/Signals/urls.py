from django.urls import path
from .views import CreateTradingSignalView

app_name = 'Signals'

urlpatterns = [
    path('create/', CreateTradingSignalView.as_view(), name='create_signal'),
]

