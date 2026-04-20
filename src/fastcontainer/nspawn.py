from pathlib import Path
from typing import List

import subprocess
import logging

from .utils import run_and_capture

logger = logging.getLogger("fastcontainer")


def _prepare_nspawn_args(
    root: Path,
    template: List[str],
    hostname: str = "fastcontainer",
    quiet: bool = True,
) -> List[str]:
    """Prepare systemd-nspawn arguments with AUTOMATIC -D root injection.

    The root directory is now always added by the runtime.
    Users are forbidden from specifying it in the YAML.
    """
    # Clean any stray manual directory flags (validation should have already caught these)
    cleaned = []
    i = 0
    while i < len(template):
        flag = str(template[i]).strip()
        if flag == "-D":
            i += 2  # skip -D + next value
            continue
        if flag.startswith(("--directory=", "-D=")) or flag == "--directory":
            i += 1
            continue
        cleaned.append(template[i])
        i += 1

    # Always start with systemd-nspawn + the correct root
    args = [cleaned[0] if cleaned else "systemd-nspawn"]
    args += ["-D", str(root)]

    # Add the rest of the user's flags
    if len(cleaned) > 0:
        args += cleaned[1:]

    # Safe defaults (never override user flags)
    if "--register=no" not in args:
        args.append("--register=no")
    if not any(a.startswith("--hostname=") for a in args):
        args.append(f"--hostname={hostname}")
    if quiet and "--quiet" not in args:
        args.append("--quiet")

    return args


def execute(root: Path, command: str, nspawn_template: List[str], verbose: bool = False) -> str:
    """Execute a command inside the container during build (output captured)."""
    strict_script = f"set -eo pipefail\n{command}"

    args = _prepare_nspawn_args(root, nspawn_template, hostname="build", quiet=True)
    args += ["/bin/bash", "-l", "-c", strict_script]

    return run_and_capture(args, verbose=verbose)


def exec_in_container(
    root: Path,
    command: List[str] | str | None,
    nspawn_template: List[str],
    verbose: bool = False,
    quiet: bool = True,
    check: bool = True,
) -> None:
    """Run a command inside an existing container (post-build or `fastcontainer exec`)."""
    if command is None or (isinstance(command, (list, str)) and not command):
        return

    args = _prepare_nspawn_args(root, nspawn_template, hostname="fastcontainer-exec", quiet=quiet)

    if isinstance(command, str):
        strict_script = f"set -eo pipefail\n{command}"
        full_cmd = args + ["/bin/bash", "-l", "-c", strict_script]
    else:
        full_cmd = args + command

    logger.debug("-> " + " ".join(map(str, full_cmd)))
    subprocess.run(full_cmd, check=check)


def check_in_container(
    root: Path, command: str | None, nspawn_template: List[str], verbose: bool = False
) -> bool:
    """Run a check snippet inside an existing container.
    Returns True if exit code == 0, False otherwise.
    """
    if not command or not command.strip():
        return True

    strict_script = f"set -eo pipefail\n{command}"

    # Check deliberately does NOT use --quiet so failure output is visible
    args = _prepare_nspawn_args(root, nspawn_template, hostname="check", quiet=False)
    args += ["/bin/bash", "-l", "-c", strict_script]

    try:
        run_and_capture(args, verbose=verbose)
        return True
    except subprocess.CalledProcessError as e:
        logger.info(f"Check failed (exit {e.returncode}) → will force rebuild")
        return False
    except Exception as e:
        logger.warning(f"Could not run check: {e} → will force rebuild")
        return False
