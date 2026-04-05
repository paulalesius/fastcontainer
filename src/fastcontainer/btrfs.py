from pathlib import Path

from .utils import run


def snapshot(src: Path | str, dest: Path | str, quiet: bool = False) -> None:
    run(["btrfs", "subvolume", "snapshot", str(src), str(dest)], quiet=quiet, capture_output=quiet)


def delete(path: Path | str, commit: bool = True, quiet: bool = False) -> None:
    cmd = ["btrfs", "subvolume", "delete"]
    if commit:
        cmd.append("-c")
    cmd.append(str(path))
    run(cmd, quiet=quiet, capture_output=quiet)
