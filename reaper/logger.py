"""Central logging — stdout only; systemd/journald captures it."""
import logging
import sys

_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
        logger.addHandler(h)
        # don't also bubble to the root logger — a library (hyperliquid SDK)
        # installs a root handler, which was double-printing every line.
        logger.propagate = False
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger
