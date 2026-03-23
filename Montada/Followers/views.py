from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from datetime import timedelta
from django.db.models import Q, Count, Avg, OuterRef, Subquery, F, Value, FloatField, ExpressionWrapper
from django.db.models.functions import Coalesce, NullIf, TruncMonth
from django.utils import timezone
from django.conf import settings as django_settings

from .models import AnalystReview, Follow, Mute

try:
    from Signals.models import TradingSignal
except ImportError:
    TradingSignal = None

try:
    from Mainapp.models import ActivityLog, UserNotification
except ImportError:
    ActivityLog = None
    UserNotification = None

from .serializers import (
    FollowSerializer,
    FollowRequestSerializer,
    FollowActionSerializer,
    MuteSerializer,
    MuteActionSerializer,
    UserMinimalSerializer,
    FollowerListItemSerializer,
    AnalystReviewSubmitSerializer,
    AnalystReviewReadSerializer,
)

User = get_user_model()


def _notify_analyst_follow(analyst_user, follower_user, title, message=None):
    """Create a UserNotification for the analyst when someone follows them."""
    if UserNotification is None:
        return
    UserNotification.objects.create(
        user=analyst_user,
        title=title,
        message=message or title,
        notification_type="SUCCESS",
    )


# ---------- Follow request / accept / reject / unfollow / block ----------


class FollowRequestView(APIView):
    """Follow a user (direct follow, no analyst acceptance). Does not require a subscription."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        if user_id == request.user.id:
            return Response(
                {"error": "You cannot follow yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = get_object_or_404(User, id=user_id)

        if Follow.objects.filter(
            follower=request.user, followed=target, status=Follow.Status.BLOCKED
        ).exists():
            return Response(
                {"error": "You cannot follow this user."},
                status=status.HTTP_403_FORBIDDEN,
            )

        existing = Follow.objects.filter(follower=request.user, followed=target).first()
        if existing:
            if existing.status == Follow.Status.ACCEPTED and existing.is_active:
                return Response(
                    {"message": "You are already following this user.", "follow": FollowSerializer(existing).data},
                    status=status.HTTP_200_OK,
                )
            if existing.status == Follow.Status.PENDING:
                existing.accept()
                self_name = (request.user.name or request.user.email or str(request.user.id))[:255]
                if ActivityLog:
                    other_name = (target.name or target.email or str(target.id))[:255]
                    ActivityLog.objects.create(
                        user=request.user,
                        type=ActivityLog.ActivityType.FOLLOW,
                        title=f"Started following {other_name}",
                        subtitle=None,
                        icon="user-add",
                        entity_type=ActivityLog.EntityType.USER,
                        metadata={"followed_id": str(target.id)},
                    )
                    ActivityLog.objects.create(
                        user=target,
                        type=ActivityLog.ActivityType.FOLLOW,
                        title=f"{self_name} started following you",
                        subtitle=None,
                        icon="user",
                        entity_type=ActivityLog.EntityType.USER,
                        metadata={"follower_id": str(request.user.id)},
                    )
                _notify_analyst_follow(target, request.user, f"{self_name} started following you")
                return Response(
                    {"message": "You are now following this user.", "follow": FollowSerializer(existing).data},
                    status=status.HTTP_200_OK,
                )
            if existing.status == Follow.Status.REJECTED or (existing.status == Follow.Status.ACCEPTED and not existing.is_active):
                existing.status = Follow.Status.ACCEPTED
                existing.is_active = True
                existing.accepted_at = timezone.now()
                existing.requested_at = timezone.now()
                existing.rejected_at = None
                existing.unfollowed_at = None
                existing.save(update_fields=["status", "is_active", "accepted_at", "requested_at", "rejected_at", "unfollowed_at"])
                self_name = (request.user.name or request.user.email or str(request.user.id))[:255]
                if ActivityLog:
                    other_name = (target.name or target.email or str(target.id))[:255]
                    ActivityLog.objects.create(
                        user=request.user,
                        type=ActivityLog.ActivityType.FOLLOW,
                        title=f"Started following {other_name}",
                        subtitle=None,
                        icon="user-add",
                        entity_type=ActivityLog.EntityType.USER,
                        metadata={"followed_id": str(target.id)},
                    )
                    ActivityLog.objects.create(
                        user=target,
                        type=ActivityLog.ActivityType.FOLLOW,
                        title=f"{self_name} started following you",
                        subtitle=None,
                        icon="user",
                        entity_type=ActivityLog.EntityType.USER,
                        metadata={"follower_id": str(request.user.id)},
                    )
                _notify_analyst_follow(target, request.user, f"{self_name} started following you")
                return Response(
                    {"message": "You are now following this user.", "follow": FollowSerializer(existing).data},
                    status=status.HTTP_200_OK,
                )
            if existing.status == Follow.Status.BLOCKED:
                return Response(
                    {"error": "You cannot follow this user."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        follow = Follow.objects.create(
            follower=request.user,
            followed=target,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        )
        follow.accepted_at = timezone.now()
        follow.save(update_fields=["accepted_at"])
        self_name = (request.user.name or request.user.email or str(request.user.id))[:255]
        if ActivityLog:
            other_name = (target.name or target.email or str(target.id))[:255]
            ActivityLog.objects.create(
                user=request.user,
                type=ActivityLog.ActivityType.FOLLOW,
                title=f"Started following {other_name}",
                subtitle=None,
                icon="user-add",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"followed_id": str(target.id)},
            )
            ActivityLog.objects.create(
                user=target,
                type=ActivityLog.ActivityType.FOLLOW,
                title=f"{self_name} started following you",
                subtitle=None,
                icon="user",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"follower_id": str(request.user.id)},
            )
        _notify_analyst_follow(target, request.user, f"{self_name} started following you")
        return Response(
            {"message": "You are now following this user.", "follow": FollowSerializer(follow).data},
            status=status.HTTP_201_CREATED,
        )


class FollowAcceptView(APIView):
    """Accept a follow request (you are the followed user). Body: { "follow_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowActionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        follow = get_object_or_404(Follow, id=serializer.validated_data["follow_id"])
        if follow.followed_id != request.user.id:
            return Response(
                {"error": "You can only accept follow requests sent to you."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if follow.status != Follow.Status.PENDING:
            return Response(
                {"error": "This request is not pending."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        follow.accept()
        follower_name = (follow.follower.name or follow.follower.email or str(follow.follower.id))[:255]
        if ActivityLog:
            followed_name = (follow.followed.name or follow.followed.email or str(follow.followed.id))[:255]
            ActivityLog.objects.create(
                user=request.user,
                type=ActivityLog.ActivityType.FOLLOW,
                title=f"Accepted follow from {follower_name}",
                subtitle=None,
                icon="user-check",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"follower_id": str(follow.follower_id)},
            )
            ActivityLog.objects.create(
                user=follow.follower,
                type=ActivityLog.ActivityType.FOLLOW,
                title=f"Your follow request was accepted by {followed_name}",
                subtitle=None,
                icon="user-check",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"followed_id": str(follow.followed_id)},
            )
        # Notify analyst (request.user is the followed/analyst) that this person is now following them
        _notify_analyst_follow(request.user, follow.follower, f"{follower_name} started following you")
        return Response(
            {"message": "Follow request accepted.", "follow": FollowSerializer(follow).data},
            status=status.HTTP_200_OK,
        )


class FollowRejectView(APIView):
    """Reject a follow request. Body: { "follow_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowActionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        follow = get_object_or_404(Follow, id=serializer.validated_data["follow_id"])
        if follow.followed_id != request.user.id:
            return Response(
                {"error": "You can only reject follow requests sent to you."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if follow.status != Follow.Status.PENDING:
            return Response(
                {"error": "This request is not pending."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        follow.reject()
        if ActivityLog:
            follower_name = (follow.follower.name or follow.follower.email or str(follow.follower.id))[:255]
            ActivityLog.objects.create(
                user=request.user,
                type=ActivityLog.ActivityType.FOLLOW,
                title=f"Rejected follow from {follower_name}",
                subtitle=None,
                icon="user-x",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"follower_id": str(follow.follower_id)},
            )
        return Response(
            {"message": "Follow request rejected.", "follow": FollowSerializer(follow).data},
            status=status.HTTP_200_OK,
        )


class UnfollowView(APIView):
    """Unfollow a user. Body: { "user_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        follow = Follow.objects.filter(
            follower=request.user,
            followed_id=user_id,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).first()
        if not follow:
            return Response(
                {"error": "You are not following this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        unfollowed_user = follow.followed
        follow.unfollow()
        if ActivityLog:
            other_name = (unfollowed_user.name or unfollowed_user.email or str(unfollowed_user.id))[:255]
            ActivityLog.objects.create(
                user=request.user,
                type=ActivityLog.ActivityType.FOLLOW,
                title=f"Unfollowed {other_name}",
                subtitle=None,
                icon="user-minus",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"unfollowed_id": str(unfollowed_user.id)},
            )
        return Response(
            {"message": "Unfollowed.", "follow": FollowSerializer(follow).data},
            status=status.HTTP_200_OK,
        )


class BlockUserView(APIView):
    """Block a user. Body: { "user_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        if user_id == request.user.id:
            return Response(
                {"error": "You cannot block yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = get_object_or_404(User, id=user_id)
        follow, created = Follow.objects.get_or_create(
            follower=target,
            followed=request.user,
            defaults={"status": Follow.Status.BLOCKED, "is_active": False},
        )
        if not created:
            follow.block()
        if ActivityLog:
            target_name = (target.name or target.email or str(target.id))[:255]
            ActivityLog.objects.create(
                user=request.user,
                type=ActivityLog.ActivityType.GENERAL,
                title=f"Blocked user {target_name}",
                subtitle=None,
                icon="user-block",
                entity_type=ActivityLog.EntityType.USER,
                metadata={"blocked_id": str(target.id)},
            )
        return Response(
            {"message": "User blocked.", "follow": FollowSerializer(follow).data},
            status=status.HTTP_200_OK,
        )


class UnblockUserView(APIView):
    """Unblock a user. Body: { "user_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        follow = Follow.objects.filter(
            follower_id=user_id,
            followed=request.user,
            status=Follow.Status.BLOCKED,
        ).first()
        if not follow:
            return Response(
                {"error": "User is not blocked."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        follow.delete()
        return Response({"message": "User unblocked."}, status=status.HTTP_200_OK)


# ---------- Mute ----------


class MuteUserView(APIView):
    """Mute a user. Body: { "user_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MuteActionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        if user_id == request.user.id:
            return Response(
                {"error": "You cannot mute yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = get_object_or_404(User, id=user_id)
        mute, created = Mute.objects.get_or_create(muter=request.user, muted=target)
        return Response(
            {"message": "User muted." if created else "User was already muted.", "mute": MuteSerializer(mute).data},
            status=status.HTTP_200_OK,
        )


class UnmuteUserView(APIView):
    """Unmute a user. Body: { "user_id": "<uuid>" }"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MuteActionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        deleted, _ = Mute.objects.filter(muter=request.user, muted_id=user_id).delete()
        if not deleted:
            return Response(
                {"error": "User is not muted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"message": "User unmuted."}, status=status.HTTP_200_OK)


# ---------- Lists ----------


class FollowersPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _base_followers_queryset(user):
    """Accepted, active follows where user is the followed (people who follow user)."""
    return Follow.objects.filter(
        followed=user,
        status=Follow.Status.ACCEPTED,
        is_active=True,
    )


def _premium_followers_filter(now):
    """Q filter for followers with an active paid plan (monthly/yearly)."""
    return Q(
        follower__subscription__status="active",
        follower__subscription__end_date__gte=now,
        follower__subscription__plan_type__in=["monthly", "yearly"],
    )


def _trial_followers_filter(now):
    """Q filter for followers on an active free_trial plan."""
    return Q(
        follower__subscription__status="active",
        follower__subscription__end_date__gte=now,
        follower__subscription__plan_type="free_trial",
    )


class FollowersListView(generics.ListAPIView):
    """
    List users who follow you (accepted and active). Uses DRF pagination: ?page=1&page_size=20.
    Response includes: count, followers_count, premium_followers_count, trial_followers_count,
    basic_followers_count, followers_this_week_count, next, previous, results.
    Query params:
      - category: 'all' (default) | 'premium' | 'trial' | 'basic'
          premium  – followers with an active monthly/yearly subscription
          trial    – followers on an active free trial
          basic    – followers with no active subscription (neither premium nor trial)
          all      – no filter (default)
      - search: optional string to filter by follower's name, username, or email.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = FollowerListItemSerializer
    pagination_class = FollowersPageNumberPagination

    def get_queryset(self):
        now = timezone.now()
        qs = (
            _base_followers_queryset(self.request.user)
            .select_related("follower")
            .annotate(applied_signals_count=Count("follower__applied_signals"))
            .order_by("-accepted_at")
        )

        category = (self.request.query_params.get("subscription_type") or "all").strip().lower()
        premium_filter = _premium_followers_filter(now)
        trial_filter = _trial_followers_filter(now)
        if category == "premium":
            qs = qs.filter(premium_filter)
        elif category == "trial":
            qs = qs.filter(trial_filter)
        elif category == "basic":
            qs = qs.exclude(premium_filter).exclude(trial_filter)

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(follower__name__icontains=search)
                | Q(follower__username__icontains=search)
                | Q(follower__email__icontains=search)
            )

        return qs

    def list(self, request, *args, **kwargs):
        now = timezone.now()
        week_ago = now - timedelta(days=7)
        base_qs = _base_followers_queryset(request.user)
        premium_filter = _premium_followers_filter(now)
        trial_filter = _trial_followers_filter(now)

        response = super().list(request, *args, **kwargs)
        response.data["followers_count"] = base_qs.count()
        response.data["premium_followers_count"] = base_qs.filter(premium_filter).count()
        response.data["trial_followers_count"] = base_qs.filter(trial_filter).count()
        response.data["basic_followers_count"] = base_qs.exclude(premium_filter).exclude(trial_filter).count()
        response.data["followers_this_week_count"] = base_qs.filter(accepted_at__gte=week_ago).count()
        return response


class FollowingListView(APIView):
    """List users you follow (accepted and active)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Follow.objects.filter(
            follower=request.user,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).select_related("followed").order_by("-accepted_at")
        users = [f.followed for f in qs]
        return Response({
            "count": len(users),
            "following": UserMinimalSerializer(users, many=True).data,
        })


class PendingReceivedListView(APIView):
    """List pending follow requests you received."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Follow.objects.filter(
            followed=request.user,
            status=Follow.Status.PENDING,
        ).select_related("follower").order_by("-requested_at")
        return Response({
            "count": qs.count(),
            "pending_requests": FollowSerializer(qs, many=True).data,
        })


class PendingSentListView(APIView):
    """List pending follow requests you sent."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Follow.objects.filter(
            follower=request.user,
            status=Follow.Status.PENDING,
        ).select_related("followed").order_by("-requested_at")
        return Response({
            "count": qs.count(),
            "pending_sent": FollowSerializer(qs, many=True).data,
        })


class MutedListView(APIView):
    """List users you have muted."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Mute.objects.filter(muter=request.user).select_related("muted").order_by("-muted_at")
        users = [m.muted for m in qs]
        return Response({
            "count": len(users),
            "muted": UserMinimalSerializer(users, many=True).data,
        })


class AnalystsListView(APIView):
    """
    List analysts for traders.
    Query params:
      - search (optional): filter by name/email/username
      - include_status (optional, 1/true/yes): add follow_status per analyst
      - sort: followers | signals | winrate | top
        - followers: top followed analysts (default)
        - signals: top by signals posted
        - winrate: top by win rate (closed signals only)
        - top: top analysts by combined score (win rate + signals count + followers count)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sort = request.query_params.get("sort", "").strip().lower() or "followers"
        valid_sorts = ("followers", "signals", "winrate", "top")
        if sort not in valid_sorts:
            sort = "followers"

        # Subquery: count accepted+active follows per analyst
        followers_subq = (
            Follow.objects.filter(
                followed_id=OuterRef("pk"),
                status=Follow.Status.ACCEPTED,
                is_active=True,
            )
            .values("followed_id")
            .annotate(c=Count("id"))
            .values("c")
        )
        # Subquery: count only non–soft-deleted signals per analyst
        if TradingSignal is not None:
            signals_subq = (
                TradingSignal.active.filter(analyst_id=OuterRef("pk"))
                .values("analyst_id")
                .annotate(c=Count("id"))
                .values("c")
            )
            # Wins/losses for closed, non-deleted signals (for winrate and top sort)
            wins_filter = Q(
                posted_signals__deleted_at__isnull=True,
                posted_signals__status=TradingSignal.Status.CLOSED,
                posted_signals__is_win=True,
            )
            losses_filter = Q(
                posted_signals__deleted_at__isnull=True,
                posted_signals__status=TradingSignal.Status.CLOSED,
                posted_signals__is_loss=True,
            )
            qs = (
                User.objects.filter(user_type="analyst", is_active=True)
                .annotate(
                    followers_count=Coalesce(Subquery(followers_subq), 0),
                    signals_count=Coalesce(Subquery(signals_subq), 0),
                    wins=Count("posted_signals", filter=wins_filter),
                    losses=Count("posted_signals", filter=losses_filter),
                )
                .annotate(
                    win_rate_pct=Coalesce(
                        ExpressionWrapper(
                            F("wins") * 100.0 / NullIf(F("wins") + F("losses"), Value(0)),
                            output_field=FloatField(),
                        ),
                        Value(0),
                        output_field=FloatField(),
                    )
                )
            )
        else:
            qs = (
                User.objects.filter(user_type="analyst", is_active=True)
                .annotate(
                    followers_count=Coalesce(Subquery(followers_subq), 0),
                    signals_count=Count(
                        "posted_signals",
                        filter=Q(posted_signals__deleted_at__isnull=True),
                    ),
                )
            )

        # Combined score for sort=top: win rate (weighted) + signals count + followers count
        if sort == "top" and TradingSignal is not None:
            qs = qs.annotate(
                top_score=ExpressionWrapper(
                    (F("win_rate_pct") * 2) + F("signals_count") + F("followers_count"),
                    output_field=FloatField(),
                )
            )

        # Apply ordering
        if sort == "followers":
            qs = qs.order_by("-followers_count", "-date_joined")
        elif sort == "signals":
            qs = qs.order_by("-signals_count", "-date_joined")
        elif sort == "winrate" and TradingSignal is not None:
            qs = qs.order_by("-win_rate_pct", "-followers_count", "-date_joined")
        elif sort == "top" and TradingSignal is not None:
            qs = qs.order_by("-top_score", "-date_joined")
        else:
            qs = qs.order_by("-date_joined")

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(username__icontains=search)
            )
        analysts = list(qs[:200])
        include_status = request.query_params.get("include_status", "").lower() in ("1", "true", "yes")
        data = UserMinimalSerializer(analysts, many=True).data
        for i, user in enumerate(analysts):
            data[i]["followers_count"] = getattr(user, "followers_count", 0)
            data[i]["signals_count"] = getattr(user, "signals_count", 0)
            # Win rate 0–100 (only when TradingSignal and wins/losses annotated)
            win_pct = getattr(user, "win_rate_pct", None)
            data[i]["win_rate_percentage"] = round(float(win_pct), 2) if win_pct is not None else None

        # Always set is_following so the requester knows whether they follow each analyst
        analyst_ids = [u.id for u in analysts]
        follow_sent = (
            Follow.objects.filter(
                follower=request.user,
                followed_id__in=analyst_ids,
            ).select_related("followed")
            if analyst_ids
            else []
        )
        follow_sent_map = {f.followed_id: f for f in follow_sent}
        for i, user in enumerate(analysts):
            f = follow_sent_map.get(user.id)
            data[i]["is_following"] = f is not None and f.status == Follow.Status.ACCEPTED and f.is_active

        if include_status and analysts:
            result = []
            for i, user in enumerate(analysts):
                row = dict(data[i])
                f = follow_sent_map.get(user.id)
                if not f:
                    row["follow_status"] = {"is_following": False, "is_pending_sent": False, "is_blocked_by_them": False}
                else:
                    row["follow_status"] = {
                        "is_following": f.status == Follow.Status.ACCEPTED and f.is_active,
                        "is_pending_sent": f.status == Follow.Status.PENDING,
                        "is_blocked_by_them": f.status == Follow.Status.BLOCKED,
                    }
                result.append(row)
            data = result
        return Response({
            "count": len(data),
            "analysts": data,
        })


# ---------- Counts ----------


class CountsView(APIView):
    """Get followers/following/pending/muted counts. Optional: ?user_id=<uuid> for another user's counts."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.query_params.get("user_id")
        if user_id:
            user = get_object_or_404(User, id=user_id)
            target = user
        else:
            target = request.user

        followers_count = Follow.objects.filter(
            followed=target,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).count()
        following_count = Follow.objects.filter(
            follower=target,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).count()
        pending_received_count = Follow.objects.filter(
            followed=target,
            status=Follow.Status.PENDING,
        ).count()
        pending_sent_count = Follow.objects.filter(
            follower=target,
            status=Follow.Status.PENDING,
        ).count()
        muted_count = Mute.objects.filter(muter=target).count() if target == request.user else 0

        return Response({
            "followers_count": followers_count,
            "following_count": following_count,
            "pending_received_count": pending_received_count,
            "pending_sent_count": pending_sent_count,
            "muted_count": muted_count,
        })


# ---------- Status for a user ----------


class FollowStatusView(APIView):
    """Get follow/mute status with respect to a user. Query: ?user_id=<uuid>"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.query_params.get("user_id")
        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = get_object_or_404(User, id=user_id)

        follow_sent = Follow.objects.filter(
            follower=request.user,
            followed=target,
        ).first()
        follow_received = Follow.objects.filter(
            follower=target,
            followed=request.user,
        ).first()

        is_following = False
        is_pending_sent = False
        is_pending_received = False
        is_blocked_by_me = False
        is_blocked_by_them = False
        is_muted = False

        if follow_sent:
            if follow_sent.status == Follow.Status.ACCEPTED and follow_sent.is_active:
                is_following = True
            elif follow_sent.status == Follow.Status.PENDING:
                is_pending_sent = True
            elif follow_sent.status == Follow.Status.BLOCKED:
                is_blocked_by_them = True
        if follow_received:
            if follow_received.status == Follow.Status.PENDING:
                is_pending_received = True
            elif follow_received.status == Follow.Status.BLOCKED:
                is_blocked_by_me = True

        is_muted = Mute.objects.filter(muter=request.user, muted=target).exists()

        return Response({
            "user_id": str(target.id),
            "is_following": is_following,
            "is_pending_sent": is_pending_sent,
            "is_pending_received": is_pending_received,
            "is_blocked_by_me": is_blocked_by_me,
            "is_blocked_by_them": is_blocked_by_them,
            "is_muted": is_muted,
        })


class AnalystPublicProfileView(APIView):
    """
    GET  /api/followers/analysts/<uuid:analyst_id>/profile/

    Returns the public profile of an analyst for trader consumption.

    Response sections:
    ─ profile         : id, username, name, profile_picture, member_since,
                        phone_number, experience, company, contact_details, social_links
    ─ stats           : total_signals, open_signals, closed_signals,
                        win_rate, loss_rate, neutral_rate,
                        avg_confidence, total_followers, total_applied
    ─ by_asset        : signal counts grouped by asset class
    ─ by_direction    : BUY / SELL split
    ─ by_timeframe    : signal counts per timeframe
    ─ monthly_graph   : last 6 months win / loss / total per month
    ─ follow_status   : is_following, follow_state (accepted/pending/none)
    """
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _profile_picture_url(request, user):
        if not user.profile_picture:
            return None
        try:
            base = getattr(django_settings, "PUBLIC_MEDIA_BASE_URL", None)
            if base:
                return f"{base.rstrip('/')}/{user.profile_picture}"
            return request.build_absolute_uri(user.profile_picture.url)
        except Exception:
            return None

    def get(self, request, analyst_id):
        User = get_user_model()

        analyst = get_object_or_404(
            User, id=analyst_id, user_type="analyst", is_active=True
        )

        if TradingSignal is None:
            return Response(
                {"error": "Signals module unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # ── base signal queryset (exclude soft-deleted) ──────────────────────
        qs = TradingSignal.active.filter(analyst=analyst)

        # Single aggregate pass for all direct-field counts → 1 DB round-trip.
        # NOTE: Count("applications") is intentionally excluded here because it
        # introduces a one-to-many JOIN that inflates every other Count("id").
        # total_applied is fetched via a separate, clean query below.
        agg = qs.aggregate(
            total_signals  = Count("id"),
            open_signals   = Count("id", filter=Q(status="OPEN")),
            closed_signals = Count("id", filter=Q(status="CLOSED")),
            win_count      = Count("id", filter=Q(is_win=True)),
            loss_count     = Count("id", filter=Q(is_loss=True)),
            neutral_count  = Count("id", filter=Q(is_neutral=True)),
            avg_confidence = Avg("confidence_level"),
        )

        total_signals  = agg["total_signals"]   or 0
        open_signals   = agg["open_signals"]    or 0
        closed_signals = agg["closed_signals"]  or 0
        win_count      = agg["win_count"]       or 0
        loss_count     = agg["loss_count"]      or 0
        neutral_count  = agg["neutral_count"]   or 0
        avg_confidence = agg["avg_confidence"]

        # Separate query to avoid JOIN inflation on the main aggregate
        try:
            from Signals.models import AppliedSignal
            total_applied = AppliedSignal.objects.filter(
                signal__analyst=analyst,
                signal__deleted_at__isnull=True,
            ).count()
        except Exception:
            total_applied = 0

        def _pct(numerator, denominator):
            if not denominator:
                return 0.0
            return round(numerator / denominator * 100, 2)

        win_rate     = _pct(win_count,     closed_signals)
        loss_rate    = _pct(loss_count,    closed_signals)
        neutral_rate = _pct(neutral_count, closed_signals)

        # ── follower count ────────────────────────────────────────────────────
        # is_active=True is required: unfollow() keeps status=ACCEPTED but
        # sets is_active=False, so filtering status alone gives wrong count.
        total_followers = Follow.objects.filter(
            followed=analyst,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).count()

        # ── signals by asset class ────────────────────────────────────────────
        by_asset = list(
            qs.values(asset_name=F("asset_class__name"))
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        # ── signals by direction ──────────────────────────────────────────────
        direction_data = dict(
            qs.values_list("direction")
            .annotate(count=Count("id"))
        )
        by_direction = {
            "BUY":  direction_data.get("BUY",  0),
            "SELL": direction_data.get("SELL", 0),
        }

        # ── signals by timeframe ──────────────────────────────────────────────
        by_timeframe = list(
            qs.values(timeframe_code=F("timeframe__code"))
            .annotate(count=Count("id"))
            .order_by("timeframe_code")
        )

        # ── monthly graph – last 6 months ─────────────────────────────────────
        now     = timezone.now()
        six_ago = now - timedelta(days=183)
        monthly_qs = (
            qs.filter(created_at__gte=six_ago)
            .annotate(month=TruncMonth("created_at"))
            .values("month")
            .annotate(
                total  = Count("id"),
                wins   = Count("id", filter=Q(is_win=True)),
                losses = Count("id", filter=Q(is_loss=True)),
            )
            .order_by("month")
        )
        monthly_graph = [
            {
                "month":    entry["month"].strftime("%b %Y"),
                "total":    entry["total"],
                "wins":     entry["wins"],
                "losses":   entry["losses"],
                "win_rate": _pct(entry["wins"], entry["total"]),
            }
            for entry in monthly_qs
        ]

        # ── follow status for the requesting trader ───────────────────────────
        follow_state = "none"
        is_following  = False
        try:
            follow = Follow.objects.get(follower=request.user, followed=analyst)
            if follow.status == Follow.Status.ACCEPTED and follow.is_active:
                # Actively following
                is_following = True
                follow_state = "accepted"
            elif follow.status == Follow.Status.ACCEPTED and not follow.is_active:
                # Previously followed, then unfollowed
                follow_state = "unfollowed"
            elif follow.status == Follow.Status.PENDING:
                follow_state = "pending"
            elif follow.status == Follow.Status.BLOCKED:
                follow_state = "blocked"
            elif follow.status == Follow.Status.REJECTED:
                follow_state = "rejected"
        except Follow.DoesNotExist:
            pass

        return Response(
            {
                "profile": {
                    "id":              str(analyst.id),
                    "username":        analyst.username,
                    "name":            analyst.name or analyst.username,
                    "profile_picture": self._profile_picture_url(request, analyst),
                    "member_since":    analyst.created_at.strftime("%b %Y"),
                    "phone_number":    getattr(analyst, "phone_number", None) or None,
                    "experience":      getattr(analyst, "experience", None) or None,
                    "company":         getattr(analyst, "company", None) or None,
                    "contact_details": getattr(analyst, "contact_details", None) or None,
                    "social_links":    getattr(analyst, "social_links", None) or None,
                },
                "stats": {
                    "total_signals":  total_signals,
                    "open_signals":   open_signals,
                    "closed_signals": closed_signals,
                    "win_count":      win_count,
                    "loss_count":     loss_count,
                    "neutral_count":  neutral_count,
                    "win_rate":       win_rate,
                    "loss_rate":      loss_rate,
                    "neutral_rate":   neutral_rate,
                    "avg_confidence": round(avg_confidence, 2) if avg_confidence else 0.0,
                    "total_followers": total_followers,
                    "total_applied":   total_applied,
                },
                "by_asset":     by_asset,
                "by_direction": by_direction,
                "by_timeframe": by_timeframe,
                "monthly_graph": monthly_graph,
                "follow_status": {
                    "is_following": is_following,
                    "state":        follow_state,
                },
            },
            status=status.HTTP_200_OK,
        )


class AnalystReviewSubmitView(APIView):
    """
    POST: Create or update the authenticated user's review for an analyst (1–5 stars).

    One row per (reviewer, analyst); submitting again updates the same review.

    Body (JSON):
        analyst_id   — UUID of the analyst
        rating       — integer 1–5 (required)
        title        — optional string (max 200)
        review_text  — optional string (max 4000)
        is_public    — optional bool (default true)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = AnalystReviewSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        analyst_id = data["analyst_id"]
        analyst = get_object_or_404(User, pk=analyst_id)

        if getattr(analyst, "user_type", "") != "analyst":
            return Response(
                {
                    "error": "Reviews can only be submitted for analyst accounts.",
                    "code": "invalid_analyst",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if analyst.id == request.user.id:
            return Response(
                {"error": "You cannot review yourself.", "code": "cannot_review_self"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        title = (data.get("title") or "").strip() or None
        review_text = (data.get("review_text") or "").strip() or None

        review, created = AnalystReview.objects.update_or_create(
            reviewer=request.user,
            analyst=analyst,
            defaults={
                "rating": data["rating"],
                "title": title,
                "review_text": review_text,
                "is_public": data.get("is_public", True),
            },
        )

        out = AnalystReviewReadSerializer(review)
        return Response(
            {
                "message": "Review saved successfully.",
                "created": created,
                "review": out.data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
