from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from datetime import timedelta
import random


class User(AbstractUser):
    """
    Custom User model extending Django's AbstractUser
    """
    USER_TYPE_CHOICES = [
        ('trader', 'Trader'),
        ('analyst', 'Analyst'),
    ]
    
    email = models.EmailField(_('email address'), unique=True)
    name = models.CharField(_('full name'), max_length=255, blank=True, null=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    profile_picture = models.ImageField(upload_to='profile_pictures/', blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    user_type = models.CharField(
        max_length=10,
        choices=USER_TYPE_CHOICES,
        default='trader',
        help_text='Whether the user is a trader or analyst'
    )
    is_subscribed = models.BooleanField(
        default=False,
        help_text='Subscription status of the user'
    )
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')
        ordering = ['-created_at']

    def __str__(self):
        return self.email


class PasswordResetOTP(models.Model):
    """
    Model to store OTP codes for password reset
    """
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)
    
    class Meta:
        verbose_name = 'Password Reset OTP'
        verbose_name_plural = 'Password Reset OTPs'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"OTP for {self.email}"
    
    @staticmethod
    def generate_otp():
        """Generate a 6-digit OTP"""
        return str(random.randint(100000, 999999))
    
    def is_expired(self, expiry_minutes=10):
        """Check if OTP has expired (default 10 minutes)"""
        expiry_time = self.created_at + timedelta(minutes=expiry_minutes)
        return timezone.now() > expiry_time
    
    def is_valid(self):
        """Check if OTP is valid (not used and not expired)"""
        return not self.is_used and not self.is_expired()


class EmailVerificationOTP(models.Model):
    """
    Model to store OTP codes for email verification
    """
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)
    
    class Meta:
        verbose_name = 'Email Verification OTP'
        verbose_name_plural = 'Email Verification OTPs'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Verification OTP for {self.email}"
    
    @staticmethod
    def generate_otp():
        """Generate a 6-digit OTP"""
        return str(random.randint(100000, 999999))
    
    def is_expired(self, expiry_minutes=10):
        """Check if OTP has expired (default 10 minutes)"""
        expiry_time = self.created_at + timedelta(minutes=expiry_minutes)
        return timezone.now() > expiry_time
    
    def is_valid(self):
        """Check if OTP is valid (not used and not expired)"""
        return not self.is_used and not self.is_expired()
