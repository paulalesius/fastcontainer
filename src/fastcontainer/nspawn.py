from pathlib import Path

from .utils import run


def execute(root: Path, command: str, quiet: bool = False) -> None:
    """Run a command inside the container using systemd-nspawn."""
    cmd = [
        "systemd-nspawn", "-D", str(root),
        "--tmpfs=/var/tmp",
        "--private-users=no",
        "--resolv-conf=replace-stub",
    ]
    if quiet:
        cmd.append("--quiet")          # ← suppresses the big banner

    cmd += ["/bin/bash", "-l", "-c", command]
    run(cmd, quiet=quiet)
