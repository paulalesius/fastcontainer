from pathlib import Path

from .utils import run


def execute(root: Path, command: str) -> None:
    """Run a command inside the container using systemd-nspawn with our exact original flags."""
    run([
        "systemd-nspawn", "-D", str(root),
        "--tmpfs=/var/tmp",
        "--private-users=no",
        "--resolv-conf=replace-stub",
        "/bin/bash", "-l", "-c", command
    ])
