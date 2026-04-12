from pathlib import Path
from typing import List

import subprocess
import logging

from .utils import run_and_capture

logger = logging.getLogger("fastcontainer")


def execute(root: Path, command: str, nspawn_template: List[str], verbose: bool = False) -> str:
    """Execute a command inside the container (used during build - captured output)."""
    strict_script = f"set -eo pipefail\n{command}"

    args = [arg.replace("{{ROOT}}", str(root)) for arg in nspawn_template]

    if "--register=no" not in args:
        args.append("--register=no")
    if not any(a.startswith("--hostname=") for a in args):
        args.append("--hostname=build")
    if "--quiet" not in args:
        args.append("--quiet")

    args += ["/bin/bash", "-l", "-c", strict_script]

    return run_and_capture(args, verbose=verbose)


def exec_in_container(root: Path, command: List[str] | str | None, nspawn_template: List[str], verbose: bool = False) -> None:
    """Run a command (post-build cmd or `fastcontainer exec`) inside an existing container.

    THIS IS THE FIX:
      • We now inherit stdin/stdout/stderr instead of piping.
      • nspawn gets a real PTY → automatic TIOCSWINSZ + SIGWINCH forwarding.
      • Correct $COLUMNS/$LINES, dynamic resize support, full ncurses support.
    """
    if command is None:
        return
    if isinstance(command, (list, str)) and not command:
        return

    args = [arg.replace("{{ROOT}}", str(root)) for arg in nspawn_template]

    if isinstance(command, str):
        strict_script = f"set -eo pipefail\n{command}"
        full_cmd = args + ["/bin/bash", "-l", "-c", strict_script]
    else:
        full_cmd = args + command

    # Good defaults (same as before)
    if "--register=no" not in args:
        full_cmd.insert(len(args) - len(command) if isinstance(command, list) else len(args), "--register=no")
    if not any(a.startswith("--hostname=") for a in args):
        full_cmd.insert(len(args) - len(command) if isinstance(command, list) else len(args), "--hostname=fastcontainer-exec")
    if "--quiet" not in args:
        full_cmd.insert(len(args) - len(command) if isinstance(command, list) else len(args), "--quiet")

    logger.debug("-> " + " ".join(map(str, full_cmd)))

    # Key change: inherit the real terminal → nspawn does the ioctl + resize handling
    subprocess.run(full_cmd, check=True)
