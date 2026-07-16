import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ModerationReport, UserBlock
from .serializers import ModerationReportSerializer, UserBlockSerializer


logger = logging.getLogger(__name__)
User = get_user_model()


class ModerationReportCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ModerationReportSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        report = serializer.save()
        return Response(
            ModerationReportSerializer(report, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
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
        if created:
            ModerationReport.objects.create(
                reporter=request.user,
                reported_user=blocked_user,
                reported_user_id_raw=str(blocked_user_id),
                content_type="user",
                reason="blocked_by_user",
                reported_at=timezone.now(),
                platform=str(request.data.get("platform") or "")[:16],
            )
        return Response(
            UserBlockSerializer(block).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
