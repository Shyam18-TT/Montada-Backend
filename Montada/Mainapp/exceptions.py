from rest_framework.views import exception_handler

from .blocked import AccountBlocked

def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None and isinstance(exc, AccountBlocked):
        response.data = {
            "code": exc.default_code,
            "detail": str(exc.detail),
        }
    return response
