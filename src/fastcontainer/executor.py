"""Execution abstraction: everything that actually runs a command.

The builder has two execution responsibilities:

- running the base-creation script on the host (in the base's temp dir)
- running systemd-nspawn (build steps, post-build cmd/shell, checks)

Both live behind the Executor interface so the whole pipeline can run
end-to-end without systemd-nspawn or root:

- NspawnExecutor     - the real systemd-nspawn backend (requires root)
- RecordingExecutor  - records every call instead of executing it
  (used by tests and --dry-run)

_prepare_nspawn_args stays a pure function (unit-testable).
"""
from __future__ import annotations

import logging
import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Union

from .utils import run_and_capture

logger = logging.getLogger("fastcontainer")

Command = Union[List[str], str, None]


def _prepare_nspawn_args(
    root: Path,
    template: List[str],
    hostname: str = "fastcontainer",
    quiet: bool = True,
    user: str = "root",
    ephemeral: bool = False,
    boot: bool = False,
) -> List[str]:
    """Prepare systemd-nspawn arguments with AUTOMATIC -D root + --user injection.
    Supports --ephemeral and --boot for final cmd/shell execution.
    """
    # Clean any stray manual directory flags
    cleaned = []
    i = 0
    while i < len(template):
        flag = str(template[i]).strip()
        if flag == "-D":
            i += 2
            continue
        if flag.startswith(("--directory=", "-D=")) or flag == "--directory":
            i += 1
            continue
        cleaned.append(template[i])
        i += 1

    # Always start with systemd-nspawn + root
    args = [cleaned[0] if cleaned else "systemd-nspawn"]
    args += ["-D", str(root)]

    if ephemeral:
        args.append("--ephemeral")

    if boot:
        args.append("--boot")

    # inject --user for this step
    if user and user != "root":
        args += [f"--user={user}"]
    else:
        args += ["--user=root"]   # explicit default

    # Add the rest of the user's flags
    if len(cleaned) > 0:
        args += cleaned[1:]

    # Safe defaults
    if "--register=no" not in args:
        args.append("--register=no")
    if not any(a.startswith("--hostname=") for a in args):
        args.append(f"--hostname={hostname}")
    if quiet and "--quiet" not in args:
        args.append("--quiet")

    return args


class Executor(ABC):
    """Everything that runs a command during a build (host + nspawn)."""

    @abstractmethod
    def create_base(self, root: Path, script: str, verbose: bool = False) -> None:
        """Run the base-creation script on the host with cwd=root."""

    @abstractmethod
    def execute(
        self, root: Path, cmd: str, nspawn: List[str], user: str = "root", verbose: bool = False
    ) -> str:
        """Execute a build step inside the container. Returns captured output."""

    @abstractmethod
    def exec_in(
        self,
        root: Path,
        command: Command,
        nspawn: List[str],
        user: str = "root",
        verbose: bool = False,
        quiet: bool = True,
        check: bool = True,
        ephemeral: bool = False,
        boot: bool = False,
    ) -> None:
        """Run a command in an existing container (post-build cmd/shell)."""

    @abstractmethod
    def check(self, root: Path, command: str, nspawn: List[str], verbose: bool = False) -> bool:
        """Run a check snippet in the container (ephemeral: never modifies the image).
        True = pass (no rebuild)."""


class RecordingExecutor(Executor):
    """Records every call instead of executing anything.

    calls entries:

    - ("create_base", script)
    - ("execute", user, cmd)
    - ("exec_in", user, command, ephemeral, boot)
    - ("check", command)

    check_result controls what check() reports (default: pass).
    """

    def __init__(self, check_result: bool = True) -> None:
        self.calls: list[tuple] = []
        self.check_result = check_result

    def create_base(self, root: Path, script: str, verbose: bool = False) -> None:
        Path(root).mkdir(parents=True, exist_ok=True)
        self.calls.append(("create_base", script))

    def execute(
        self, root: Path, cmd: str, nspawn: List[str], user: str = "root", verbose: bool = False
    ) -> str:
        self.calls.append(("execute", user, cmd))
        lines = [line for line in cmd.strip().splitlines() if line.strip()]
        return "".join(line + "\n" for line in lines) if lines else ""

    def exec_in(
        self,
        root: Path,
        command: Command,
        nspawn: List[str],
        user: str = "root",
        verbose: bool = False,
        quiet: bool = True,
        check: bool = True,
        ephemeral: bool = False,
        boot: bool = False,
    ) -> None:
        if command is None or (isinstance(command, (list, str)) and not command):
            return
        self.calls.append(("exec_in", user, command, ephemeral, boot))

    def check(self, root: Path, command: str, nspawn: List[str], verbose: bool = False) -> bool:
        if not command or not command.strip():
            return True
        self.calls.append(("check", command))
        return self.check_result


class NspawnExecutor(Executor):
    """Real systemd-nspawn backend. Requires root."""

    def __init__(self) -> None:
        if os.geteuid() != 0:
            raise PermissionError("NspawnExecutor requires root (use sudo)")

    def create_base(self, root: Path, script: str, verbose: bool = False) -> None:
        run_and_capture(["/bin/bash", "-c", script], verbose=verbose, cwd=root)

    def execute(
        self, root: Path, cmd: str, nspawn: List[str], user: str = "root", verbose: bool = False
    ) -> str:
        """Execute a command inside the container during build (respects per-step user)."""
        strict_script = f"set -eo pipefail\n{cmd}"

        args = _prepare_nspawn_args(root, nspawn, hostname="build", quiet=True, user=user, ephemeral=False)
        args += ["/bin/bash", "-l", "-c", strict_script]

        return run_and_capture(args, verbose=verbose)

    def exec_in(
        self,
        root: Path,
        command: Command,
        nspawn: List[str],
        user: str = "root",
        verbose: bool = False,
        quiet: bool = True,
        check: bool = True,
        ephemeral: bool = False,
        boot: bool = False,
    ) -> None:
        """Run a command inside an existing container (post-build or exec)."""
        if command is None or (isinstance(command, (list, str)) and not command):
            return

        args = _prepare_nspawn_args(root, nspawn, hostname="fastcontainer-exec", quiet=quiet, user=user, ephemeral=ephemeral, boot=boot)

        if isinstance(command, str):
            strict_script = f"set -eo pipefail\n{command}"
            full_cmd = args + ["/bin/bash", "-l", "-c", strict_script]
        else:
            full_cmd = args + command

        logger.debug("-> " + " ".join(map(str, full_cmd)))
        subprocess.run(full_cmd, check=check)

    def check(self, root: Path, command: str, nspawn: List[str], verbose: bool = False) -> bool:
        """Run a check snippet in an ephemeral copy of the container.
        The cached image is never modified.
        Returns True if exit code == 0, False otherwise.
        """
        if not command or not command.strip():
            return True

        strict_script = f"set -eo pipefail\n{command}"

        # Check deliberately does NOT use --quiet so failure output is visible.
        # It runs in an ephemeral container: a check validates the cached image
        # and must never be able to modify it.
        args = _prepare_nspawn_args(root, nspawn, hostname="check", quiet=False, ephemeral=True, boot=False)
        args += ["/bin/bash", "-l", "-c", strict_script]

        try:
            run_and_capture(args, verbose=verbose)
            return True
        except subprocess.CalledProcessError as e:
            logger.info(f"Check failed (exit {e.returncode}) - will force rebuild")
            return False
        except Exception as e:
            logger.warning(f"Could not run check: {e} - will force rebuild")
            return False
