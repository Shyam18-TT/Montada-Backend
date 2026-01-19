from django.urls import path
from .views import (
    SubscriptionStatusView,
    subscribe_view,
    cancel_subscription_view,
    check_subscription_status_view
)

app_name = 'Subscriptions'

urlpatterns = [
    path('status/', SubscriptionStatusView.as_view(), name='subscription_status'),
    path('subscribe/', subscribe_view, name='subscribe'),
    path('cancel/', cancel_subscription_view, name='cancel_subscription'),
    path('check/', check_subscription_status_view, name='check_subscription'),
]

