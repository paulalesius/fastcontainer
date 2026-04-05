from pathlib import Path

from .utils import run


def execute(root: Path, command: str, quiet: bool = False) -> None:
    """Run a command inside the container using systemd-nspawn."""
    cmd = [
        "systemd-nspawn", "-D", str(root),
        "--tmpfs=/var/tmp",
        "--private-users=no",
        "--resolv-conf=replace-stub",
        "--timezone=off",          # ← silences the common /etc/localtime warning
    ]
    if quiet:
        cmd.append("--quiet")      # suppresses nspawn banner

    cmd += ["/bin/bash", "-l", "-c", command]

    # Key change: when quiet=True we capture the RUN output (apt etc.)
    # and only show it if the command fails.
    run(cmd, quiet=quiet, capture_output=quiet)
