import logging
import sys
from typing import Optional

import structlog

from app.core.config import get_settings


def setup_logging() -> structlog.BoundLogger:
    get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    return structlog.get_logger()


def get_logger(
    service: str, account_id: Optional[int] = None, task_id: Optional[int] = None
) -> structlog.BoundLogger:
    logger = structlog.get_logger(service=service)
    if account_id:
        logger = logger.bind(account_id=account_id)
    if task_id:
        logger = logger.bind(task_id=task_id)
    return logger
