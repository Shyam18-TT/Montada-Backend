from django.urls import path

from .views import ModerationReportCreateView, UserBlockCreateView


app_name = "Moderation"

urlpatterns = [
    path("reports/", ModerationReportCreateView.as_view(), name="report_create"),
    path("blocks/", UserBlockCreateView.as_view(), name="block_create"),
   
]

