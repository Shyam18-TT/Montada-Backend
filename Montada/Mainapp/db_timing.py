import logging
import time
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone


logger = logging.getLogger("db.timing")
exception_logger = logging.getLogger("app.exceptions")


def _timestamp():
    return timezone.now().isoformat(timespec="microseconds")


def _epoch_to_iso(value):
    return datetime.fromtimestamp(value, tz=dt_timezone.utc).isoformat(timespec="microseconds")


def log_manual_timing(label, start, end=None, **fields):
    end = time.time() if end is None else end
    duration = end - start
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    message = (
        f"MANUAL_TIMING label={label} "
        f"start={_epoch_to_iso(start)} "
        f"end={_epoch_to_iso(end)} "
        f"duration_seconds={duration:.6f}"
    )
    if extras:
        message = f"{message} {extras}"
    logger.info(message)


def log_manual_exception(label, **fields):
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    message = f"MANUAL_EXCEPTION label={label} time={_timestamp()}"
    if extras:
        message = f"{message} {extras}"
    exception_logger.exception(message)
