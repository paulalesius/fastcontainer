import subprocess
import sys
from typing import List

import logging
logger = logging.getLogger("fastcontainer")

def run(
    cmd: List[str],
    quiet: bool = False,
    capture_output: bool = False,
) -> None:
    """Execute a command safely.

    In quiet mode the command itself is only logged at DEBUG level
    (so the "→ btrfs ..." arrows disappear).
    """
    if not quiet:
        logger.debug("→ " + " ".join(map(str, cmd)))

    if quiet and capture_output:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Command failed: {' '.join(map(str, cmd))}")
            if result.stdout.strip():
                sys.stderr.write(result.stdout)
            if result.stderr.strip():
                sys.stderr.write(result.stderr)
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
    else:
        subprocess.run(cmd, check=True)
