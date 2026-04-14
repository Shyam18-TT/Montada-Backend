from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from .serializers import (
    UserRegistrationSerializer,
    UserLoginSerializer,
    UserProfileSerializer,
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    VerifyOTPSerializer,
    ResetPasswordSerializer,
    EmailVerificationSerializer,
    DeleteAccountConfirmSerializer,
)
from .models import PasswordResetOTP, EmailVerificationOTP, AccountDeletionOTP, DeviceToken

User = get_user_model()

# Import Subscription model for free trial creation
try:
    from Subscriptions.models import Subscription
except ImportError:
    Subscription = None


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        # Set username from email automatically
        data = request.data.copy()
        email = data.get('email')
        
        if not email:
            return Response({
                'error': 'Email is required.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if 'username' not in data or not data.get('username'):
            data['username'] = email
        
        # Check if user with this email already exists
        try:
            existing_user = User.objects.get(email=email)

            if getattr(existing_user, 'is_soft_deleted', False):
                serializer = self.get_serializer(existing_user, data=data, partial=True)
                serializer.is_valid(raise_exception=True)
                user = serializer.save()
                user.username = user.username or user.email
                user.is_active = True
                user.is_soft_deleted = False
                user.soft_deleted_at = None
                user.is_verified = False
                user.save(
                    update_fields=[
                        'username',
                        'is_active',
                        'is_soft_deleted',
                        'soft_deleted_at',
                        'is_verified',
                        'updated_at',
                    ]
                )

                otp = EmailVerificationOTP.generate_otp()
                EmailVerificationOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
                AccountDeletionOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
                EmailVerificationOTP.objects.create(email=user.email, otp=otp)

                subject = 'Email Verification OTP - Montada'
                message = f'''
Hello {user.name or user.username},

Your Montada account has been restored. Please verify your email address to activate the account again.

Your verification OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not request this, please ignore this email.

Best regards,
Montada Team
                '''

                try:
                    send_mail(
                        subject,
                        message,
                        settings.EMAIL_HOST_USER if hasattr(settings, 'EMAIL_HOST_USER') else 'noreply@montada.com',
                        [user.email],
                        fail_silently=False,
                    )
                except Exception:
                    pass

                return Response({
                    'message': 'Soft-deleted account restored. Please check your email for verification OTP to complete registration.',
                    'email': user.email
                }, status=status.HTTP_200_OK)
            
            # If user is already verified, return error
            if existing_user.is_verified:
                return Response({
                    'error': 'A user with this email already exists.',
                    'detail': 'Username/email already exists. Please login instead.'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # User exists but is not verified - update details and resend OTP
            serializer = self.get_serializer(existing_user, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            
            # Update user fields (including password if provided)
            user = serializer.save()
            user.is_verified = False
            user.save()
            
            # Generate new OTP for email verification
            otp = EmailVerificationOTP.generate_otp()
            
            # Invalidate previous OTPs for this email
            EmailVerificationOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
            
            # Create new OTP record
            otp_obj = EmailVerificationOTP.objects.create(email=user.email, otp=otp)
            
            # Send email with OTP
            subject = 'Email Verification OTP - Montada'
            message = f'''
Hello {user.name or user.username},

You have requested to update your registration details. Please verify your email address to complete your registration.

Your verification OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not request this, please ignore this email.

Best regards,
Montada Team
            '''
            
            try:
                send_mail(
                    subject,
                    message,
                    settings.EMAIL_HOST_USER if hasattr(settings, 'EMAIL_HOST_USER') else 'noreply@montada.com',
                    [user.email],
                    fail_silently=False,
                )
            except Exception as e:
                # Log the error in production, but still allow registration
                pass
            
            return Response({
                'message': 'Registration details updated. Please check your email for verification OTP to complete registration.',
                'email': user.email
            }, status=status.HTTP_200_OK)
            
        except User.DoesNotExist:
            # User doesn't exist - create new user
            pass
        
        # Create new user
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Ensure user is not verified initially
        user.is_verified = False
        user.save()
        
        # Generate OTP for email verification
        otp = EmailVerificationOTP.generate_otp()
        
        # Invalidate previous OTPs for this email
        EmailVerificationOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
        
        # Create new OTP record
        otp_obj = EmailVerificationOTP.objects.create(email=user.email, otp=otp)
        
        # Send email with OTP
        subject = 'Email Verification OTP - Montada'
        message = f'''
Hello {user.name or user.username},

Welcome to Montada! Please verify your email address to complete your registration.

Your verification OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not create an account with us, please ignore this email.

Best regards,
Montada Team
        '''
        
        try:
            send_mail(
                subject,
                message,
                settings.EMAIL_HOST_USER if hasattr(settings, 'EMAIL_HOST_USER') else 'noreply@montada.com',
                [user.email],
                fail_silently=False,
            )
        except Exception as e:
            # Log the error in production, but still allow registration
            # The user can request a new OTP later
            pass
        
        return Response({
            'message': 'Registration successful. Please check your email for verification OTP to complete registration.',
            'email': user.email
        }, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def login_view(request):

    serializer = UserLoginSerializer(data=request.data, context={'request': request})
    
    if serializer.is_valid():
        user = serializer.validated_data['user']
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        
        return Response({
            'user': UserProfileSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'message': 'Login successful'
        }, status=status.HTTP_200_OK)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserProfileView(generics.RetrieveUpdateAPIView):
    
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class ChangePasswordView(APIView):
    """
    Change password for the authenticated user.
    Requires old_password to verify identity; then sets new_password.
    POST/PUT body: { "old_password": "...", "new_password": "...", "new_password2": "..." }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        return self._change_password(request)

    def put(self, request):
        return self._change_password(request)

    def patch(self, request):
        return self._change_password(request)

    def _change_password(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {'message': 'Password changed successfully.'},
            status=status.HTTP_200_OK,
        )


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def logout_view(request):
    """
    API endpoint for user logout (blacklist refresh token)
    """
    try:
        refresh_token = request.data.get('refresh_token')
        if refresh_token:
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response({
                'message': 'Logout successful'
            }, status=status.HTTP_200_OK)
        else:
            return Response({
                'error': 'Refresh token is required'
            }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({
            'error': 'Invalid token'
        }, status=status.HTTP_400_BAD_REQUEST)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def forgot_password_view(request):
    """
    API endpoint for forgot password - sends OTP to user's email
    """
    serializer = ForgotPasswordSerializer(data=request.data)
    
    if serializer.is_valid():
        email = serializer.validated_data['email']
        
        # Check if user exists (but don't reveal if they don't for security)
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Return success message even if user doesn't exist (security best practice)
            return Response({
                'message': 'User email not exists. Please register with the email provided!'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Generate OTP
        otp = PasswordResetOTP.generate_otp()
        
        # Invalidate previous OTPs for this email
        PasswordResetOTP.objects.filter(email=email, is_used=False).update(is_used=True)
        
        # Create new OTP record
        otp_obj = PasswordResetOTP.objects.create(email=email, otp=otp)
        
        # Send email with OTP
        subject = 'Password Reset OTP - Montada'
        message = f'''
Hello,

You have requested to reset your password for your Montada account.

Your OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not request this password reset, please ignore this email.

Best regards,
Montada Team
        '''
        
        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL if hasattr(settings, 'DEFAULT_FROM_EMAIL') else 'noreply@montada.com',
                [email],
                fail_silently=False,
            )
            
            return Response({
                'message': 'OTP has been sent to your email address.'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            # Log the error in production
            return Response({
                'error': 'Failed to send email. Please try again later.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def verify_otp_view(request):
    """
    API endpoint for verifying OTP
    """
    serializer = VerifyOTPSerializer(data=request.data)
    
    if serializer.is_valid():
        otp_obj = serializer.validated_data['otp_obj']
        
        return Response({
            'message': 'OTP verified successfully.',
            'email': otp_obj.email
        }, status=status.HTTP_200_OK)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def reset_password_view(request):
    """
    API endpoint for resetting password with OTP
    """
    serializer = ResetPasswordSerializer(data=request.data)
    
    if serializer.is_valid():
        user = serializer.validated_data['user']
        otp_obj = serializer.validated_data['otp_obj']
        new_password = serializer.validated_data['new_password']
        
        # Set new password
        user.set_password(new_password)
        user.save()
        
        # Mark OTP as used
        otp_obj.is_used = True
        otp_obj.save()
        
        # Invalidate all other OTPs for this email
        PasswordResetOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
        
        return Response({
            'message': 'Password has been reset successfully.'
        }, status=status.HTTP_200_OK)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def verify_email_view(request):
    """
    API endpoint for verifying email with OTP
    """
    serializer = EmailVerificationSerializer(data=request.data)
    
    if serializer.is_valid():
        user = serializer.validated_data['user']
        otp_obj = serializer.validated_data['otp_obj']
        
        if user.is_verified:
            return Response(
                {'error': 'Email is already verified.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Mark email as verified
        user.is_verified = True
        user.save()
        
        # Mark OTP as used
        otp_obj.is_used = True
        otp_obj.save()
        
        # Invalidate all other OTPs for this email
        EmailVerificationOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)

        # Create a free trial only for users who are still eligible
        if Subscription and getattr(user, "free_trial_eligible", True):
            try:
                Subscription.create_free_trial(user)
            except Exception as e:
                # Log error but don't fail registration
                pass
        
        # Generate JWT tokens after email verification
        refresh = RefreshToken.for_user(user)
        
        return Response({
            'message': 'Email verified successfully.',
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'user': UserProfileSerializer(user).data
        }, status=status.HTTP_200_OK)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def resend_verification_otp_view(request):
    """
    API endpoint for resending email verification OTP
    """
    email = request.data.get('email')
    
    if not email:
        return Response({
            'error': 'Email is required.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({
            'error': 'User with this email does not exist.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    if user.is_verified:
        return Response({
            'message': 'Email is already verified.'
        }, status=status.HTTP_200_OK)
    
    # Generate new OTP
    otp = EmailVerificationOTP.generate_otp()
    
    # Invalidate previous OTPs for this email
    EmailVerificationOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
    
    # Create new OTP record
    otp_obj = EmailVerificationOTP.objects.create(email=user.email, otp=otp)
    
    # Send email with OTP
    subject = 'Email Verification OTP - Montada'
    message = f'''
Hello {user.name or user.username},

You have requested a new verification code for your Montada account.

Your verification OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not request this, please ignore this email.

Best regards,
Montada Team
    '''
    
    try:
        send_mail(
            subject,
            message,
            settings.EMAIL_HOST_USER if hasattr(settings, 'EMAIL_HOST_USER') else 'noreply@montada.com',
            [user.email],
            fail_silently=False,
        )
        
        return Response({
            'message': 'Verification OTP has been sent to your email address.'
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({
            'error': 'Failed to send email. Please try again later.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def resend_password_reset_otp_view(request):
    """
    API endpoint for resending password reset OTP
    """
    email = request.data.get('email')
    
    if not email:
        return Response({
            'error': 'Email is required.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Check if user exists (but don't reveal if they don't for security)
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        # Return success message even if user doesn't exist (security best practice)
        return Response({
            'message': 'If an account exists with this email, an OTP has been sent.'
        }, status=status.HTTP_200_OK)
    
    # Generate new OTP
    otp = PasswordResetOTP.generate_otp()
    
    # Invalidate previous OTPs for this email
    PasswordResetOTP.objects.filter(email=email, is_used=False).update(is_used=True)
    
    # Create new OTP record
    otp_obj = PasswordResetOTP.objects.create(email=email, otp=otp)
    
    # Send email with OTP
    subject = 'Password Reset OTP - Montada'
    message = f'''
Hello {user.name or user.username},

You have requested a new password reset code for your Montada account.

Your OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not request this password reset, please ignore this email.

Best regards,
Montada Team
    '''
    
    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL if hasattr(settings, 'DEFAULT_FROM_EMAIL') else 'noreply@montada.com',
            [email],
            fail_silently=False,
        )
        
        return Response({
            'message': 'Password reset OTP has been sent to your email address.'
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({
            'error': 'Failed to send email. Please try again later.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



class SaveFCMToken(APIView):
    """
    POST: Save FCM device token for the authenticated user.
    Body: { "token": "<fcm_token_string>" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        token = (request.data.get("token") or "").strip()
        if not token:
            return Response(
                {"error": "Token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        with transaction.atomic():
            token_rows = list(
                DeviceToken.objects.select_for_update()
                .filter(fcm_token=token)
                .order_by("created_at", "id")
            )

            if token_rows:
                device_token = token_rows[0]
                if device_token.user_id != user.id:
                    device_token.user = user
                    device_token.save(update_fields=["user"])
                if len(token_rows) > 1:
                    DeviceToken.objects.filter(
                        id__in=[row.id for row in token_rows[1:]]
                    ).delete()
            else:
                device_token = DeviceToken.objects.create(
                    user=user,
                    fcm_token=token,
                )

        return Response(
            {"message": "Token saved", "token_id": str(device_token.id)},
            status=status.HTTP_200_OK,
        )


class RequestAccountDeletionOTPView(APIView):
    """
    POST: Send an OTP to the authenticated user's email for soft-delete confirmation.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        if getattr(user, "is_soft_deleted", False):
            return Response(
                {"error": "Account is already deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp = AccountDeletionOTP.generate_otp()
        AccountDeletionOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
        AccountDeletionOTP.objects.create(email=user.email, otp=otp)

        subject = 'Delete Account OTP - Montada'
        message = f'''
Hello {user.name or user.username},

You requested to delete your Montada account.

Your OTP code is: {otp}

This OTP will expire in 10 minutes.

If you did not request this, please ignore this email.

Best regards,
Montada Team
        '''

        try:
            send_mail(
                subject,
                message,
                settings.EMAIL_HOST_USER if hasattr(settings, 'EMAIL_HOST_USER') else 'noreply@montada.com',
                [user.email],
                fail_silently=False,
            )
        except Exception:
            return Response(
                {"error": "Failed to send email. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"message": "Account deletion OTP has been sent to your email address."},
            status=status.HTTP_200_OK,
        )


class ConfirmAccountDeletionView(APIView):
    """
    POST: Validate OTP and soft-delete the authenticated user's account.
    Body: { "otp": "123456" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = DeleteAccountConfirmSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
        otp_obj = serializer.validated_data['otp_obj']

        with transaction.atomic():
            user.is_soft_deleted = True
            user.soft_deleted_at = timezone.now()
            user.is_active = False
            user.is_verified = False
            user.save(update_fields=['is_soft_deleted', 'soft_deleted_at', 'is_active', 'is_verified', 'updated_at'])

            otp_obj.is_used = True
            otp_obj.save(update_fields=['is_used'])

            AccountDeletionOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)
            DeviceToken.objects.filter(user=user).delete()

        return Response(
            {"message": "Account deleted successfully."},
            status=status.HTTP_200_OK,
        )




class TestJson(APIView):
    permission_classes = [permissions.AllowAny]
    def get(self, request):
        return Response({'message':"Response from the server"})