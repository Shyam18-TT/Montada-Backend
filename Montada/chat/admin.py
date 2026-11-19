from django.contrib import admin
from .models import Conversation, ConversationParticipant, ChatMessage


class ConversationParticipantInline(admin.TabularInline):
    model = ConversationParticipant
    extra = 0


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "is_group", "name", "created_by", "is_active", "created_at", "updated_at")
    list_filter = ("is_group", "is_active", "only_admins_can_send")
    search_fields = ("name",)
    inlines = [ConversationParticipantInline]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "sender", "created_at", "read_at", "is_deleted")
    list_filter = ("is_deleted", "created_at")
    search_fields = ("content",)
