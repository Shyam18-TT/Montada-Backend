from rest_framework import serializers
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from .models import User


class UserRegistrationSerializer(serializers.ModelSerializer):
    
    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password]
    )

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'password', 
                  'name', 'phone_number', 'date_of_birth', 
                  'user_type', 'is_subscribed')
        extra_kwargs = {
            'username': {'required': False},
            'name': {'required': False},
            'phone_number': {'required': False},
            'date_of_birth': {'required': False},
            'user_type': {'required': True},
            'is_subscribed': {'required': False},
        }

    def create(self, validated_data):
        # Ensure username is set from email if not provided
        if 'username' not in validated_data or not validated_data.get('username'):
            validated_data['username'] = validated_data['email']
        user = User.objects.create_user(**validated_data)
        return user


class UserLoginSerializer(serializers.Serializer):
    
    email = serializers.EmailField(required=True)
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        if email and password:
            user = authenticate(request=self.context.get('request'),
                              username=email, password=password)
            if not user:
                raise serializers.ValidationError(
                    'Unable to log in with provided credentials.'
                )
            if not user.is_active:
                raise serializers.ValidationError(
                    'User account is disabled.'
                )
            attrs['user'] = user
            return attrs
        else:
            raise serializers.ValidationError(
                'Must include "email" and "password".'
            )


class UserProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for user profile
    """
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'name',
                  'phone_number', 'profile_picture', 'date_of_birth',
                  'user_type', 'is_subscribed', 'is_verified', 
                  'created_at', 'updated_at')
        read_only_fields = ('id', 'email', 'is_verified', 'created_at', 'updated_at')


class ChangePasswordSerializer(serializers.Serializer):
    """
    Serializer for password change
    """
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password]
    )
    new_password2 = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password2']:
            raise serializers.ValidationError(
                {"new_password": "Password fields didn't match."}
            )
        return attrs

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Old password is incorrect.")
        return value

    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save()
        return user


class ForgotPasswordSerializer(serializers.Serializer):
    """
    Serializer for forgot password - sends OTP to email
    """
    email = serializers.EmailField(required=True)
    
    def validate_email(self, value):
        """Check if user with this email exists"""
        from .models import User
        try:
            user = User.objects.get(email=value)
            if not user.is_active:
                raise serializers.ValidationError("User account is disabled.")
        except User.DoesNotExist:
            # Don't reveal if email exists or not for security
            pass
        return value


class VerifyOTPSerializer(serializers.Serializer):
    """
    Serializer for verifying OTP
    """
    email = serializers.EmailField(required=True)
    otp = serializers.CharField(required=True, max_length=6, min_length=6)
    
    def validate(self, attrs):
        from .models import PasswordResetOTP
        email = attrs.get('email')
        otp = attrs.get('otp')
        
        try:
            otp_obj = PasswordResetOTP.objects.filter(
                email=email,
                otp=otp,
                is_used=False
            ).order_by('-created_at').first()
            
            if not otp_obj:
                raise serializers.ValidationError(
                    {"otp": "Invalid OTP or email."}
                )
            
            if not otp_obj.is_valid():
                raise serializers.ValidationError(
                    {"otp": "OTP has expired. Please request a new one."}
                )
            
            attrs['otp_obj'] = otp_obj
            return attrs
        except Exception as e:
            raise serializers.ValidationError(
                {"otp": "Invalid OTP or email."}
            )


class ResetPasswordSerializer(serializers.Serializer):
    """
    Serializer for resetting password with OTP
    """
    email = serializers.EmailField(required=True)
    otp = serializers.CharField(required=True, max_length=6, min_length=6)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password]
    )
    
    def validate(self, attrs):
        from .models import PasswordResetOTP, User
        
        
        email = attrs.get('email')
        otp = attrs.get('otp')
        
        # Verify OTP
        try:
            otp_obj = PasswordResetOTP.objects.filter(
                email=email,
                otp=otp,
                is_used=False
            ).order_by('-created_at').first()
            
            if not otp_obj:
                raise serializers.ValidationError(
                    {"otp": "Invalid OTP or email."}
                )
            
            if not otp_obj.is_valid():
                raise serializers.ValidationError(
                    {"otp": "OTP has expired. Please request a new one."}
                )
            
            # Check if user exists
            try:
                user = User.objects.get(email=email)
                attrs['user'] = user
                attrs['otp_obj'] = otp_obj
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"email": "User with this email does not exist."}
                )
            
            return attrs
        except serializers.ValidationError:
            raise
        except Exception as e:
            raise serializers.ValidationError(
                {"otp": "Invalid OTP or email."}
            )
