# src/fastcontainer/log.py
import logging
import sys

def setup_logger(quiet: bool = False) -> logging.Logger:
    """Configure a clean logger for fastcontainer.

    Progress messages (INFO) are always shown.
    Tool commands (DEBUG) are hidden when quiet=True.
    """
    # Clean output format (no timestamps, just the message)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
        force=True,          # re-configure if called again
    )

    logger = logging.getLogger("fastcontainer")
    logger.setLevel(logging.DEBUG if not quiet else logging.INFO)
    return logger
