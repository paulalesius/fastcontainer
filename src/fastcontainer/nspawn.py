from pathlib import Path
from typing import List

from .utils import run_and_capture, run


def execute(root: Path, command: str, nspawn_template: List[str], quiet: bool = False) -> str:
    """Execute a command inside the container using the selected profile's nspawn template.

    Automatically enables strict error handling so a failing command
    in a multi-line RUN stops the build immediately.
    """
    # Prepend strict mode (same as Docker/Buildah do)
    strict_script = f"set -eo pipefail\n{command}"

    # Replace placeholder {{ROOT}} with actual container root path
    args = [arg.replace("{{ROOT}}", str(root)) for arg in nspawn_template]

    # Always quiet nspawn itself — we control output
    if "--quiet" not in args:
        args.append("--quiet")

    args += ["/bin/bash", "-l", "-c", strict_script]

    return run_and_capture(args, quiet=quiet)


def exec_in_container(root: Path, command: List[str] | str | None, nspawn_template: List[str], quiet: bool = False) -> None:
    """Run a command inside an existing container using its stored nspawn template.

    `command` supports two modes (exactly like RUN steps):
    - List[str]: direct argv (CLI override -- no shell, perfect for arguments with spaces)
    - str (or YAML `cmd: |` block): free-form shell script (multi-line commands, pipes, etc.)
    - None: no-op
    """
    if command is None:
        return
    if isinstance(command, (list, str)) and not command:
        return

    # Replace {{ROOT}} placeholder
    args = [arg.replace("{{ROOT}}", str(root)) for arg in nspawn_template]

    if quiet and "--quiet" not in args:
        args.append("--quiet")

    if isinstance(command, str):
        # free-form script mode (new `cmd: |` support)
        strict_script = f"set -eo pipefail\n{command}"
        full_cmd = args + ["/bin/bash", "-l", "-c", strict_script]
    else:
        # original argv mode
        full_cmd = args + command

    run(full_cmd, quiet=quiet)
