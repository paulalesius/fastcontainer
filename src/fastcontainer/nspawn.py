from pathlib import Path

from .utils import run

def execute(root: Path, command: str, quiet: bool = False) -> None:
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
    run(cmd, quiet=quiet, capture_output=quiet)
