import subprocess
import sys
from typing import List
from pathlib import Path

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
        logger.debug("-> " + " ".join(map(str, cmd)))

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

def run_and_capture(
    cmd: List[str],
    quiet: bool = False,
    cwd: Path | str | None = None,
) -> str:
    """Run a command with live output (if not quiet) and always return the full output.

    Used for RUN steps (inside nspawn) and base creation (on host).
    """
    import sys
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,           # line-buffered
        cwd=cwd,
    )
    output_lines: list[str] = []
    for line in process.stdout:  # type: ignore
        output_lines.append(line)
        if not quiet:
            sys.stdout.write(line)
            sys.stdout.flush()
    returncode = process.wait()
    output = "".join(output_lines)

    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output)

    return output
