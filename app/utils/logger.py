"""Centralized logger factory."""

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


@lru_cache(maxsize=None)
def get_logger(name: str = "chatbot") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger
