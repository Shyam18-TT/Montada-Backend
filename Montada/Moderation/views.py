import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ModerationReport, UserBlock
from .serializers import ModerationReportSerializer, UserBlockSerializer


logger = logging.getLogger(__name__)
User = get_user_model()


def _send_moderation_report_email(report):
    subject = f"New moderation report: {report.reason}"
    message = f'''
A new moderation report has been submitted. Please review and take appropriate action.

Report ID: {report.id}
Reporter: {report.reporter_id}
Reported user: {report.reported_user_id or 'deleted'}
Content type: {report.content_type}
Content ID: {report.content_id}
Reason: {report.reason}
Platform: {report.platform}
Reported at: {report.reported_at}

Excerpt:
{report.content_excerpt}

Details:
{report.details}

Best regards,
Montada Team
        '''
    try:
        send_mail(
            subject,
            message,
            settings.EMAIL_HOST_USER if hasattr(settings, 'EMAIL_HOST_USER') else 'noreply@montada.com',
            ['sumsubtestmail@gmail.com'],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send moderation report email.")


class ModerationReportCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ModerationReportSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        report = serializer.save()

        _send_moderation_report_email(report)

        return Response(
            ModerationReportSerializer(report, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )



class UserBlockCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = UserBlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        blocked_user_id = serializer.validated_data["blocked_user_id"]
        try:
            blocked_user = User.objects.get(pk=blocked_user_id)
        except (User.DoesNotExist, DjangoValidationError, TypeError, ValueError):
            return Response(
                {"error": "Blocked user not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if blocked_user.pk == request.user.pk:
            return Response(
                {"error": "You cannot block yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        block, created = UserBlock.objects.get_or_create(
            blocker=request.user,
            blocked=blocked_user,
        )
        
        return Response(
            UserBlockSerializer(block).data,
            status=status.HTTP_200_OK,
        )
