import uuid
from django.db import models
from django.conf import settings




class PollQuestion(models.Model):
    QUESTION_TYPES = (
        ("single", "Single Choice"),
        ("multiple", "Multiple Choice"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


    question_text = models.TextField()

    question_type = models.CharField(
        max_length=20,
        choices=QUESTION_TYPES,
        default="single"
    )

    order = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True, help_text="If False, poll is closed and no new votes accepted.")

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



