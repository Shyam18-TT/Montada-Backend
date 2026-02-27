import uuid
from django.db import models
from django.conf import settings

class Poll(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)

    allow_multiple_answers = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title



class PollQuestion(models.Model):
    QUESTION_TYPES = (
        ("single", "Single Choice"),
        ("multiple", "Multiple Choice"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    poll = models.ForeignKey(
        Poll,
        on_delete=models.CASCADE,
        related_name="questions"
    )

    question_text = models.TextField()

    question_type = models.CharField(
        max_length=20,
        choices=QUESTION_TYPES,
        default="single"
    )

    order = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.question_text


class PollOption(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(
        PollQuestion,
        on_delete=models.CASCADE,
        related_name="options"
    )

    option_text = models.CharField(max_length=255)

    def __str__(self):
        return self.option_text


    
class PollResponse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="responses")
    question = models.ForeignKey(PollQuestion, on_delete=models.CASCADE, related_name="responses")
    option = models.ForeignKey(PollOption, on_delete=models.CASCADE, related_name="responses")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="poll_responses"
    )

    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "question", "option")



