# src/fastcontainer/log.py
import logging
import sys

def setup_logger(verbose: bool = False) -> logging.Logger:
    """Clean ASCII-only logger for fastcontainer.

    - Normal mode: clean progress messages only (INFO+)
    - Verbose mode: full debug + command output
    """
    level = logging.DEBUG if verbose else logging.INFO

    # Very clean formatter (no timestamps, no extra noise)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )
    logger = logging.getLogger("fastcontainer")
    logger.setLevel(level)
    return logger
