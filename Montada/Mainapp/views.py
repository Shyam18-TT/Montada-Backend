from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.conf import settings
from .serializers import (
    UserRegistrationSerializer,
    UserLoginSerializer,
    UserProfileSerializer,
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    VerifyOTPSerializer,
    ResetPasswordSerializer,
    EmailVerificationSerializer
)
from .models import PasswordResetOTP, EmailVerificationOTP

User = get_user_model()

# Import Subscription model for free trial creation
try:
    from Subscriptions.models import Subscription
except ImportError:
    Subscription = None


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


class ChangePasswordView(generics.UpdateAPIView):
    """
    API endpoint for changing password
    """
    serializer_class = ChangePasswordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def update(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response({
            'message': 'Password changed successfully'
        }, status=status.HTTP_200_OK)


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
        
        # Mark email as verified
        user.is_verified = True
        user.save()
        
        # Mark OTP as used
        otp_obj.is_used = True
        otp_obj.save()
        
        # Invalidate all other OTPs for this email
        EmailVerificationOTP.objects.filter(email=user.email, is_used=False).update(is_used=True)

         # Create 7-day free trial subscription for new user
        if Subscription:
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
