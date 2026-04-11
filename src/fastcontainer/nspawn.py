from pathlib import Path
from typing import List

from .utils import run_and_capture


def execute(root: Path, command: str, nspawn_template: List[str], verbose: bool = False) -> str:
    """Execute a command inside the container using the profile's nspawn template.

    Strict error handling so a failing command stops the build immediately.
    Output control is delegated to run_and_capture.
    """
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
    """Run a command (post-build cmd or CLI exec) inside an existing container.

    Always shows output for user commands (post-build / exec).
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

    # User-facing commands (post-build or exec) always show output
    run_and_capture(full_cmd, verbose=True)
