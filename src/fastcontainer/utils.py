import subprocess
import sys
from typing import List
from pathlib import Path

import logging
logger = logging.getLogger("fastcontainer")


def run(
    cmd: List[str],
) -> None:
    """Execute internal command (btrfs, etc.). Always silent on success."""
    logger.debug("-> " + " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def run_and_capture(
    cmd: List[str],
    verbose: bool = False,
    cwd: Path | str | None = None,
) -> str:
    """Run a command, capture output always.

    - verbose=True: live output (for -v mode)
    - On failure: ALWAYS print the full step output (even without -v)
      so the user sees exactly what went wrong.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    output_lines: list[str] = []
    for line in process.stdout:  # type: ignore
        output_lines.append(line)
        if verbose:
            sys.stdout.write(line)
            sys.stdout.flush()

    returncode = process.wait()
    output = "".join(output_lines)

    if returncode != 0:
        if not verbose:
            # Critical: show failing step output even in clean mode
            sys.stdout.write("\n--- BUILD STEP FAILED ---\n")
            sys.stdout.write(output)
            sys.stdout.write("--- END OF FAILED STEP OUTPUT ---\n\n")
            sys.stdout.flush()
        raise subprocess.CalledProcessError(returncode, cmd, output)

    return output
