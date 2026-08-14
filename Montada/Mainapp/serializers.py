from rest_framework import serializers
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from .blocked import AccountBlocked, is_account_blocked
from .models import User, AccountDeletionOTP


NEWS_LANGUAGE_PREFERENCE_FIELDS = (
    'news_notify_ar',
    'news_notify_en',
    'news_notify_zh',
)
DEFAULT_NEWS_LANGUAGE_PREFERENCES = {
    'news_notify_ar': False,
    'news_notify_en': True,
    'news_notify_zh': False,
}
NEWS_LANGUAGE_OPTIONS = (
    {'code': 'ar', 'label': 'Arabic'},
    {'code': 'en', 'label': 'English'},
    {'code': 'zh', 'label': 'Chinese'},
)


def _apply_news_language_preferences(attrs, *, instance=None):
    provided_fields = [field for field in NEWS_LANGUAGE_PREFERENCE_FIELDS if field in attrs]
    if not provided_fields:
        if instance is None:
            attrs.update(DEFAULT_NEWS_LANGUAGE_PREFERENCES)
        return attrs

    current_preferences = {
        field: getattr(instance, field, DEFAULT_NEWS_LANGUAGE_PREFERENCES[field])
        if instance is not None
        else DEFAULT_NEWS_LANGUAGE_PREFERENCES[field]
        for field in NEWS_LANGUAGE_PREFERENCE_FIELDS
    }
    for field in provided_fields:
        current_preferences[field] = bool(attrs[field])

    if sum(1 for field in NEWS_LANGUAGE_PREFERENCE_FIELDS if current_preferences[field]) < 1:
        raise serializers.ValidationError(
            {'news_language_preferences': 'Choose any news notification languages.'}
        )

    attrs.update(current_preferences)
    return attrs


def _selected_news_language_codes(user):
    selected = []
    if getattr(user, 'news_notify_ar', False):
        selected.append('ar')
    if getattr(user, 'news_notify_en', False):
        selected.append('en')
    if getattr(user, 'news_notify_zh', False):
        selected.append('zh')
    return selected


class NewsLanguagePreferenceSerializerMixin(serializers.Serializer):
    news_notification_languages = serializers.SerializerMethodField(read_only=True)
    news_notification_language_options = serializers.SerializerMethodField(read_only=True)
    news_notification_selection_limit = serializers.SerializerMethodField(read_only=True)

    def get_news_notification_languages(self, obj):
        return _selected_news_language_codes(obj)

    def get_news_notification_language_options(self, obj):
        return list(NEWS_LANGUAGE_OPTIONS)

    def get_news_notification_selection_limit(self, obj):
        return 2


class UserRegistrationSerializer(NewsLanguagePreferenceSerializerMixin, serializers.ModelSerializer):
    
    password = serializers.CharField(
        write_only=True,
        required=False,
        validators=[validate_password],
        allow_blank=True
    )

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'password',
            'name', 'phone_number', 'date_of_birth',
            'user_type', 'is_subscribed',
            'news_notify_ar', 'news_notify_en', 'news_notify_zh',
            'news_notification_languages',
            'news_notification_language_options',
            'news_notification_selection_limit',
            'experience', 'company', 'contact_details', 'social_links',
        )
        extra_kwargs = {
            'username': {'required': False},
            'name': {'required': False},
            'phone_number': {'required': False},
            'date_of_birth': {'required': False},
            'user_type': {'required': True},
            'is_subscribed': {'required': False},
            'news_notify_ar': {'required': False},
            'news_notify_en': {'required': False},
            'news_notify_zh': {'required': False},
            'experience': {'required': False},
            'company': {'required': False},
            'contact_details': {'required': False},
            'social_links': {'required': False},
        }

    def validate(self, attrs):
        # Password is required for new user creation
        # For updates (when instance exists), password is optional
        if not self.instance and not attrs.get('password'):
            raise serializers.ValidationError({
                'password': 'Password is required for registration.'
            })
        return _apply_news_language_preferences(attrs, instance=self.instance)

    def create(self, validated_data):
        # Ensure username is set from email if not provided
        if 'username' not in validated_data or not validated_data.get('username'):
            validated_data['username'] = validated_data['email']
        user = User.objects.create_user(**validated_data)
        return user
    
    def update(self, instance, validated_data):
        # Handle password separately
        password = validated_data.pop('password', None)
        
        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Update password if provided
        if password:
            instance.set_password(password)
        
        instance.save()
        return instance


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
            existing_user = User.objects.filter(email=email).first()
            if existing_user and getattr(existing_user, 'is_soft_deleted', False):
                raise serializers.ValidationError(
                    'This account has been deleted. Please register again to restore it.'
                )
            if existing_user and is_account_blocked(existing_user):
                raise AccountBlocked()
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


class UserProfileSerializer(NewsLanguagePreferenceSerializerMixin, serializers.ModelSerializer):
    """
    Serializer for user profile. Includes analyst fields when user_type is analyst.
    """
    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'name',
            'phone_number', 'profile_picture', 'date_of_birth',
            'user_type', 'is_subscribed', 'is_verified',
            'news_notify_ar', 'news_notify_en', 'news_notify_zh',
            'news_notification_languages',
            'news_notification_language_options',
            'news_notification_selection_limit',
            'experience', 'company', 'contact_details', 'social_links',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'email', 'is_verified', 'created_at', 'updated_at')

    def validate(self, attrs):
        return _apply_news_language_preferences(attrs, instance=self.instance)


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


class EmailVerificationSerializer(serializers.Serializer):
    """
    Serializer for verifying email with OTP
    """
    email = serializers.EmailField(required=True)
    otp = serializers.CharField(required=True, max_length=6, min_length=6)
    
    def validate(self, attrs):
        from .models import EmailVerificationOTP, User
        
        email = attrs.get('email')
        otp = attrs.get('otp')
        
        # Verify OTP
        try:
            otp_obj = EmailVerificationOTP.objects.filter(
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


class DeleteAccountConfirmSerializer(serializers.Serializer):
    """
    Serializer for confirming account deletion with OTP.
    """
    otp = serializers.CharField(required=True, max_length=6, min_length=6)

    def validate(self, attrs):
        user = self.context['request'].user
        otp = attrs.get('otp')

        otp_obj = AccountDeletionOTP.objects.filter(
            email=user.email,
            otp=otp,
            is_used=False,
        ).order_by('-created_at').first()

        if not otp_obj:
            raise serializers.ValidationError(
                {"otp": "Invalid OTP."}
            )

        if not otp_obj.is_valid():
            raise serializers.ValidationError(
                {"otp": "OTP has expired. Please request a new one."}
            )

        attrs['otp_obj'] = otp_obj
        return attrs
