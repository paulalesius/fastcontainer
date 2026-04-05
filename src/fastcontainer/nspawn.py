from pathlib import Path

from .utils import run_and_capture

def execute(root: Path, command: str, quiet: bool = False) -> str:
    """Execute a command inside the container and return its full output (for logging)."""
    cmd = [
        "systemd-nspawn", "-D", str(root),
        "--tmpfs=/var/tmp",
        "--private-users=no",
        "--resolv-conf=replace-stub",
        "--timezone=off",
    ]
    if quiet:
        cmd.append("--quiet")

    cmd += ["/bin/bash", "-l", "-c", command]

    return run_and_capture(cmd, quiet=quiet)
