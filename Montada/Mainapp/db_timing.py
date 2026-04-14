import logging
import time
from datetime import datetime, timezone as dt_timezone

from django.db import connections
from django.db.backends.signals import connection_created
from django.utils import timezone


logger = logging.getLogger("db.timing")
exception_logger = logging.getLogger("app.exceptions")
_SIGNAL_UID = "montada-db-timing-logger"


def _timestamp():
    return timezone.now().isoformat(timespec="microseconds")


def _epoch_to_iso(value):
    return datetime.fromtimestamp(value, tz=dt_timezone.utc).isoformat(timespec="microseconds")


def log_manual_timing(label, start, end=None, **fields):
    """
    Manually log timing details to django_db.log.

    Example:
        start = time.time()
        # logic
        log_manual_timing("login_view", start)
    """
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
    """
    Log the current exception traceback to logs/exception.log.

    Example:
        try:
            ...
        except Exception:
            log_manual_exception("login_view", user_id=request.user.id)
            raise
    """
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    message = f"MANUAL_EXCEPTION label={label} time={_timestamp()}"
    if extras:
        message = f"{message} {extras}"
    exception_logger.exception(message)


def _query_wrapper(execute, sql, params, many, context):
    alias = getattr(context.get("connection"), "alias", "default")
    logger.info("QUERY_START alias=%s time=%s", alias, _timestamp())
    try:
        return execute(sql, params, many, context)
    finally:
        logger.info("QUERY_END alias=%s time=%s", alias, _timestamp())


def _install_wrapper(connection):
    if getattr(connection, "_montada_db_timing_installed", False):
        return
    connection.execute_wrappers.append(_query_wrapper)
    connection._montada_db_timing_installed = True


def _on_connection_created(sender, connection, **kwargs):
    logger.info("CONNECTION_ESTABLISHED alias=%s time=%s", connection.alias, _timestamp())
    _install_wrapper(connection)


def setup_db_timing_logging():
    connection_created.connect(_on_connection_created, dispatch_uid=_SIGNAL_UID)
    for connection in connections.all():
        if connection.connection is not None:
            _install_wrapper(connection)
