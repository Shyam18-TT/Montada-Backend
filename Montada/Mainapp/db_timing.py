import logging

from django.db import connections
from django.db.backends.signals import connection_created
from django.utils import timezone


logger = logging.getLogger("db.timing")
_SIGNAL_UID = "montada-db-timing-logger"


def _timestamp():
    return timezone.now().isoformat(timespec="microseconds")


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
