from django.urls import path
from .views import (
    SubscriptionStatusView,
    subscribe_view,
    cancel_subscription_view,
    check_subscription_status_view,
    confirm_subscription_view,
    StripeAnalyticsView,
    AnalystPlanRevenueView,
)
from .analyst_plan_views import (
    AnalystContentPlanListCreateView,
    AnalystContentPlanDetailView,
    PublicAnalystContentPlanCatalogView,
    SubscribeToAnalystPlanView,
    MyAnalystPlanSubscriptionsListView,
    CancelAnalystPlanSubscriptionView,
)

app_name = 'Subscriptions'

urlpatterns = [
    path('status/', SubscriptionStatusView.as_view(), name='subscription_status'),
    path('subscribe/', subscribe_view, name='subscribe'),
    path('confirm/', confirm_subscription_view, name='confirm_subscription'),
    path('cancel/', cancel_subscription_view, name='cancel_subscription'),
    path('check/', check_subscription_status_view, name='check_subscription'),
    path('stripe-analytics/', StripeAnalyticsView.as_view(), name='stripe_analytics'),
    # -------------------------------------------------------------------------
    # Per-analyst content plans (separate from app-wide Subscription above)
    # -------------------------------------------------------------------------
    # Public catalog: active plans for one analyst (any authenticated user)
    path(
        'public/analyst/<uuid:analyst_id>/plans/',
        PublicAnalystContentPlanCatalogView.as_view(),
        name='public_analyst_plans_catalog',
    ),
    # Trader: subscribe to a plan (cannot subscribe to own plan)
    path(
        'trader/subscribe-to-analyst-plan/',
        SubscribeToAnalystPlanView.as_view(),
        name='trader_subscribe_analyst_plan',
    ),
    # Original paths (same views; kept for backward compatibility)
    path(
        'analyst-content-plans/',
        AnalystContentPlanListCreateView.as_view(),
        name='analyst_content_plan_list_create',
    ),
    path(
        'analyst-content-plans/catalog/<uuid:analyst_id>/',
        PublicAnalystContentPlanCatalogView.as_view(),
        name='analyst_content_plan_catalog',
    ),
    path(
        'analyst-content-plans/subscribe/',
        SubscribeToAnalystPlanView.as_view(),
        name='analyst_content_plan_subscribe',
    ),
    path(
        'analyst-content-plans/my-subscriptions/',
        MyAnalystPlanSubscriptionsListView.as_view(),
        name='analyst_content_plan_my_subscriptions',
    ),
    path(
        'analyst-content-plans/my-subscriptions/<uuid:pk>/cancel/',
        CancelAnalystPlanSubscriptionView.as_view(),
        name='analyst_content_plan_subscription_cancel',
    ),
    path(
        'analyst-content-plans/<uuid:pk>/',
        AnalystContentPlanDetailView.as_view(),
        name='analyst_content_plan_detail',
    ),
    path(
        'analyst-content-plans/revenue/',
        AnalystPlanRevenueView.as_view(),
        name='analyst_plan_revenue',
    ),
]

