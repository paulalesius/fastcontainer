# src/fastcontainer/log.py
import logging
import sys

def setup_logger(verbose: bool = False) -> logging.Logger:
    """Configure a clean logger for fastcontainer.

    Default (no -v): clean ASCII progress messages only (INFO).
    With -v/--verbose: full step output + internal debug messages.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )
    logger = logging.getLogger("fastcontainer")
    logger.setLevel(level)
    return logger
