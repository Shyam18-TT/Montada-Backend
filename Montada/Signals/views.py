from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from .models import TradingSignal, AssetClass, Instrument, Timeframe
from .serializers import (
    TradingSignalSerializer,
    AssetClassSerializer,
    InstrumentSerializer,
    AssetClassWithInstrumentsSerializer,
    TimeframeSerializer,
    TimeframeSimpleSerializer
)


class IsAnalystPermission(permissions.BasePermission):
    """
    Custom permission to only allow analyst users to post signals
    """
    def has_permission(self, request, view):
        # Check if user is authenticated
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Check if user is an analyst
        return request.user.user_type == 'analyst'


class AnalystSignalPagination(PageNumberPagination):
    """
    Custom pagination for analyst signals list
    Returns 10 signals per page
    """
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class CreateTradingSignalView(generics.CreateAPIView):
    """
    API endpoint for analysts to create trading signals
    Only analyst users can post signals
    """
    queryset = TradingSignal.active.all()
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    
    def perform_create(self, serializer):
        # Automatically set the analyst to the current authenticated user
        serializer.save(analyst=self.request.user)
    
    def create(self, request, *args, **kwargs):
        # Check if user is an analyst
        if request.user.user_type != 'analyst':
            return Response({
                'error': 'Only analyst users can post trading signals.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        
        return Response({
            'message': 'Trading signal created successfully.',
            'signal': serializer.data
        }, status=status.HTTP_201_CREATED)


class AssetClassListView(generics.ListAPIView):
    """
    API endpoint to list all active asset classes
    """
    queryset = AssetClass.objects.filter(is_active=True)
    serializer_class = AssetClassSerializer
    permission_classes = [permissions.IsAuthenticated]


class InstrumentListView(generics.ListAPIView):
    """
    API endpoint to list all active instruments
    Can be filtered by asset_class
    """
    serializer_class = InstrumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        queryset = Instrument.objects.filter(is_active=True)
        asset_class_id = self.request.query_params.get('asset_class', None)
        
        if asset_class_id:
            queryset = queryset.filter(asset_class_id=asset_class_id)
        
        return queryset


class TimeframeListView(generics.ListAPIView):
    """
    API endpoint to list all active timeframes
    Returns only id, code, and name without pagination
    """
    queryset = Timeframe.objects.filter(is_active=True)
    serializer_class = TimeframeSimpleSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None  # Disable pagination


class AssetClassWithInstrumentsView(generics.ListAPIView):
    """
    API endpoint to get all asset classes with their related instruments in a single response
    No pagination - returns all results at once
    """
    queryset = AssetClass.objects.filter(is_active=True).prefetch_related('instruments')
    serializer_class = AssetClassWithInstrumentsSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None  # Disable pagination for this view
    
    def get_queryset(self):
        """
        Optimize query by prefetching instruments
        """
        return AssetClass.objects.filter(
            is_active=True
        ).prefetch_related('instruments').order_by('name')


class AnalystSignalListView(generics.ListAPIView):
    """
    API endpoint for analysts to view all signals created by them
    Only analyst users can access this endpoint
    Returns all signals created by the authenticated analyst user
    Paginated to 10 signals per page
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    pagination_class = AnalystSignalPagination
    
    def get_queryset(self):
        """
        Filter signals to only return those created by the current analyst user
        Excludes soft-deleted signals
        """
        if not self.request.user.is_authenticated or self.request.user.user_type != 'analyst':
            return TradingSignal.active.none()

        queryset = TradingSignal.active.filter(
            analyst=self.request.user
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe'
        )

        # Optional filter by status query parameter: ?status=OPEN/CLOSED/DRAFT
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        return queryset.order_by('-created_at')


class AnalystSignalUpdateView(generics.RetrieveUpdateAPIView):
    """
    API endpoint for analysts to retrieve and update a specific signal
    Only the analyst who created the signal can edit it
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    
    def get_queryset(self):
        """
        Filter signals to only return those created by the current analyst user
        Excludes soft-deleted signals
        """
        if not self.request.user.is_authenticated or self.request.user.user_type != 'analyst':
            return TradingSignal.active.none()
        
        return TradingSignal.active.filter(
            analyst=self.request.user
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe'
        )
    
    def update(self, request, *args, **kwargs):
        """
        Handle signal update with proper response
        """
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        
        return Response({
            'message': 'Trading signal updated successfully.',
            'signal': serializer.data
        }, status=status.HTTP_200_OK)


class AnalystSignalSoftDeleteView(generics.RetrieveAPIView):
    """
    API endpoint for analysts to soft delete a specific signal
    Only the analyst who created the signal can delete it
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    
    def get_queryset(self):
        """
        Filter signals to only return those created by the current analyst user
        Excludes already soft-deleted signals
        """
        if not self.request.user.is_authenticated or self.request.user.user_type != 'analyst':
            return TradingSignal.active.none()
        
        return TradingSignal.active.filter(
            analyst=self.request.user
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe'
        )
    
    def delete(self, request, *args, **kwargs):
        """
        Soft delete the signal by setting deleted_at timestamp
        """
        instance = self.get_object()
        instance.soft_delete()
        
        return Response({
            'message': 'Trading signal deleted successfully.'
        }, status=status.HTTP_200_OK)