import logging

from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import transaction
from django.db.models import Q
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
from .emails import send_templated_email, user_display_name
from .audit import audit

logger = logging.getLogger(__name__)

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
                    send_templated_email(
                        subject,
                        [user.email],
                        'acc-restored.html',
                        {'name': user_display_name(user), 'otp': otp},
                        message,
                    )
                except Exception:
                    pass

                audit("auth.account_restored", request=request, actor=user, user=user.pk)
                return Response({
                    'message': 'Soft-deleted account restored. Please check your email for verification OTP to complete registration.',
                    'email': user.email,
                    'user': UserProfileSerializer(user).data,
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
                send_templated_email(
                    subject,
                    [user.email],
                    'reg-details-updated.html',
                    {'name': user_display_name(user), 'otp': otp},
                    message,
                )
            except Exception as e:
                # Still allow registration; the failure is recorded for follow-up.
                audit("auth.verification_email_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)

            audit("auth.register_updated_unverified", request=request, actor=user, user=user.pk, user_type=user.user_type)
            return Response({
                'message': 'Registration details updated. Please check your email for verification OTP to complete registration.',
                'email': user.email,
                'user': UserProfileSerializer(user).data,
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
            send_templated_email(
                subject,
                [user.email],
                'verify-email.html',
                {'name': user_display_name(user), 'otp': otp},
                message,
            )
        except Exception as e:
            # Still allow registration; the user can request a new OTP later.
            audit("auth.verification_email_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)

        audit("auth.registered", request=request, actor=user, user=user.pk, user_type=user.user_type)
        return Response({
            'message': 'Registration successful. Please check your email for verification OTP to complete registration.',
            'email': user.email,
            'user': UserProfileSerializer(user).data,
        }, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def login_view(request):
    import time
    from Mainapp.db_timing import log_manual_exception, log_manual_timing

    start = time.time()
    email = request.data.get('email')

    try:
        serializer = UserLoginSerializer(data=request.data, context={'request': request})
        
        if serializer.is_valid():
            user = serializer.validated_data['user']
            
            # Generate JWT tokens
            refresh = RefreshToken.for_user(user)
            audit("auth.login_success", request=request, actor=user, user=user.pk, user_type=user.user_type)
            log_manual_timing(
                "login_view_success",
                start,
                email=email,
                user_id=user.id,
            )
            
            return Response({
                'user': UserProfileSerializer(user).data,
                'tokens': {
                    'refresh': str(refresh),
                    'access': str(refresh.access_token),
                },
                'message': 'Login successful'
            }, status=status.HTTP_200_OK)

        audit(
            "auth.login_failed", request=request, level=logging.WARNING,
            email=email, reason=dict(serializer.errors),
        )
        log_manual_timing("login_view_invalid", start, email=email)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        log_manual_exception("login_view_exception", email=email)
        raise


class UserProfileView(generics.RetrieveUpdateAPIView):
    
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def perform_update(self, serializer):
        serializer.save()
        # Field names only; values (phone, DOB, ...) are personal data.
        audit(
            "user.profile_updated", request=self.request, user=self.request.user.pk,
            fields=",".join(sorted(serializer.validated_data)),
        )


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
        audit("auth.password_changed", request=request, user=request.user.pk)
        return Response(
            {'message': 'Password changed successfully.'},
            status=status.HTTP_200_OK,
        )


def _user_id_from_access_header(request):
    """User id of a valid Bearer access token, or None (missing, malformed or expired)."""
    from rest_framework_simplejwt.authentication import JWTAuthentication

    try:
        result = JWTAuthentication().authenticate(request)
    except Exception:
        return None
    return result[0].pk if result else None


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def logout_view(request):
    """
    Sign out: blacklist the refresh token and unregister this device from push.
    Body: { "refresh_token": "...", "device_id": "..." (optional) }

    Works with an expired (or missing) access token: the account is identified from the access
    token when it is valid and always from the refresh token. The device row is removed only when
    it belongs to one of those accounts, so a logout can never remove another user's device.
    Without device_id (a queued logout replayed after someone signed in again) only the refresh
    token is blacklisted.
    """
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.settings import api_settings as jwt_settings
    from rest_framework_simplejwt.state import token_backend

    from .push_devices import unregister_device

    refresh_token = request.data.get('refresh_token')
    refresh_token = refresh_token.strip() if isinstance(refresh_token, str) else ''
    if not refresh_token:
        return Response({'error': 'Refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)

    # Verify signature and expiry, but not the blacklist: replaying a logout must still unregister.
    try:
        payload = token_backend.decode(refresh_token, verify=True)
        if payload.get('token_type') != 'refresh':
            raise TokenError('Not a refresh token')
        refresh_user_id = payload.get(jwt_settings.USER_ID_CLAIM)
    except Exception as exc:
        audit("auth.logout_failed", request=request, level=logging.WARNING, error=type(exc).__name__)
        return Response({'error': 'Invalid token'}, status=status.HTTP_401_UNAUTHORIZED)

    user_ids = {uid for uid in (_user_id_from_access_header(request), refresh_user_id) if uid}
    device_id = str(request.data.get('device_id') or '').strip()
    fcm_token = str(request.data.get('fcm_token') or request.data.get('token') or '').strip()
    removed = 0
    try:
        removed = unregister_device(
            [str(uid) for uid in user_ids], device_id=device_id, token=fcm_token,
        ) if (device_id or fcm_token) else 0
    except Exception:
        logger.exception("Logout: unregistering push device failed.")

    try:
        RefreshToken(refresh_token).blacklist()
    except TokenError:
        pass  # already blacklisted or expired between decode and blacklist: the goal is met
    except Exception as exc:
        audit("auth.logout_failed", request=request, level=logging.ERROR, error=type(exc).__name__)
        return Response({'error': 'Logout failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    audit("auth.logout", request=request, user=str(refresh_user_id), device_id=device_id or None,
          devices_removed=removed)
    return Response({'message': 'Logout successful'}, status=status.HTTP_200_OK)


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
            audit("auth.password_reset_requested_unknown_email", request=request, level=logging.WARNING, email=email)
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
            send_templated_email(
                subject,
                [email],
                'forgot-psw.html',
                {'name': user_display_name(user), 'otp': otp},
                message,
            )
            
            audit("auth.password_reset_otp_sent", request=request, actor=user, user=user.pk)
            return Response({
                'message': 'OTP has been sent to your email address.'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            audit("auth.password_reset_otp_email_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)
            # Log the error in production
            print(f"Exception on forget password is {str(e)}")
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

        audit("auth.password_reset", request=request, actor=user, user=user.pk)
        return Response({
            'message': 'Password has been reset successfully.'
        }, status=status.HTTP_200_OK)

    audit("auth.password_reset_failed", request=request, level=logging.WARNING, reason=dict(serializer.errors))
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
                audit("subscription.free_trial_created", request=request, actor=user, user=user.pk)
            except Exception as e:
                # Don't fail registration, but record it: the user ends up without a trial.
                audit("subscription.free_trial_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)

        # Generate JWT tokens after email verification
        refresh = RefreshToken.for_user(user)
        audit("auth.email_verified", request=request, actor=user, user=user.pk)

        return Response({
            'message': 'Email verified successfully.',
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'user': UserProfileSerializer(user).data
        }, status=status.HTTP_200_OK)

    audit("auth.email_verification_failed", request=request, level=logging.WARNING, reason=dict(serializer.errors))
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
        send_templated_email(
            subject,
            [user.email],
            'verify-email.html',
            {'name': user_display_name(user), 'otp': otp},
            message,
        )
        
        audit("auth.verification_otp_resent", request=request, actor=user, user=user.pk)
        return Response({
            'message': 'Verification OTP has been sent to your email address.'
        }, status=status.HTTP_200_OK)
    except Exception as e:
        audit("auth.verification_email_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)
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
        send_templated_email(
            subject,
            [email],
            'forgot-psw.html',
            {'name': user_display_name(user), 'otp': otp},
            message,
        )
        
        audit("auth.password_reset_otp_resent", request=request, actor=user, user=user.pk)
        return Response({
            'message': 'Password reset OTP has been sent to your email address.'
        }, status=status.HTTP_200_OK)
    except Exception as e:
        audit("auth.password_reset_otp_email_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)
        return Response({
            'error': 'Failed to send email. Please try again later.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



class SaveFCMToken(APIView):
    """
    POST: register this device for push notifications.
    Body: { "token": "<fcm token>", "device_id": "<stable device id>", "platform": "android"|"ios" }
    device_id and platform are optional (older app builds omit them); unknown fields are ignored.
    Idempotent: repeating the call leaves one row. See Mainapp/push_devices.py for the rules.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .push_devices import MAX_TOKEN_LENGTH, register_device

        token = request.data.get("token")
        token = token.strip() if isinstance(token, str) else ""
        if not token:
            return Response(
                {"error": "Token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(token) > MAX_TOKEN_LENGTH:
            return Response(
                {"error": "Token is too long."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        device, moved_from = register_device(
            user,
            token,
            device_id=request.data.get("device_id"),
            platform=request.data.get("platform"),
        )

        audit("device.fcm_token_saved", request=request, user=user.pk, device_id=device.device_id or None,
              platform=device.platform, token_id=device.id,
              moved_from_user=str(moved_from) if moved_from else None)
        return Response(
            {
                "registered": True,
                "device_id": device.device_id,
                "platform": device.platform,
                # Kept for older clients.
                "message": "Token saved",
                "token_id": str(device.id),
            },
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
            send_templated_email(
                subject,
                [user.email],
                'delete-acc-conf.html',
                {'name': user_display_name(user), 'otp': otp},
                message,
            )
        except Exception as e:
            audit("account.deletion_otp_email_failed", request=request, level=logging.ERROR, user=user.pk, error=type(e).__name__)
            return Response(
                {"error": "Failed to send email. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        audit("account.deletion_otp_sent", request=request, user=user.pk)
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

        audit("account.deleted_by_user", request=request, user=user.pk, user_type=user.user_type)
        return Response(
            {"message": "Account deleted successfully."},
            status=status.HTTP_200_OK,
        )




class TestJson(APIView):
    permission_classes = [permissions.AllowAny]
    def get(self, request):
        return Response({'message':"Response from the server"})

class NotificationPreferencesView(APIView):
    """
    Per-category push notification settings for the authenticated user.
    In-app notifications are always created and have no setting.

    GET: every category the user can configure, with its current mode.
        {
          "modes": ["sound", "silent", "off"],
          "preferences": [
            {"category": "NEWS", "label": "News", "description": "...",
             "mode": "sound", "enabled": true, "sound": true},
            ...
          ]
        }
    PUT / PATCH: change one or more categories (others are left as they are). Body:
        {"preferences": {"NEWS": "silent", "MESSAGES": "off"}}
      or
        {"preferences": [{"category": "NEWS", "enabled": true, "sound": false}]}
      Returns the same payload as GET.

    Modes: "sound" = push with sound (default), "silent" = push without sound,
    "off" = no push for that category.
    """
    permission_classes = [permissions.IsAuthenticated]

    def _payload(self, user):
        from .notification_preferences import CATEGORIES, MODES, OFF, SOUND, categories_for_user, get_user_modes

        modes = get_user_modes(user)
        return {
            "modes": list(MODES),
            "preferences": [
                {
                    "category": key,
                    "label": CATEGORIES[key]["label"],
                    "description": CATEGORIES[key]["description"],
                    "mode": modes[key],
                    "enabled": modes[key] != OFF,
                    "sound": modes[key] == SOUND,
                }
                for key in categories_for_user(user)
            ],
        }

    def get(self, request):
        return Response(self._payload(request.user), status=status.HTTP_200_OK)

    def put(self, request):
        from .notification_preferences import save_user_modes
        from .serializers import NotificationPreferencesUpdateSerializer

        serializer = NotificationPreferencesUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        save_user_modes(request.user, serializer.validated_data["preferences"])
        audit("user.notification_preferences_updated", request=request, user=request.user.pk,
              categories=",".join(serializer.validated_data["preferences"]))
        return Response(self._payload(request.user), status=status.HTTP_200_OK)

    def patch(self, request):
        return self.put(request)
