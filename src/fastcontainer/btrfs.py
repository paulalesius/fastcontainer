from pathlib import Path

from .utils import run


def snapshot(src: Path | str, dest: Path | str) -> None:
    """Create a btrfs subvolume snapshot."""
    run(["btrfs", "subvolume", "snapshot", str(src), str(dest)])


def delete(path: Path | str, commit: bool = True) -> None:
    """Delete a btrfs subvolume (with optional commit)."""
    cmd = ["btrfs", "subvolume", "delete"]
    if commit:
        cmd.append("-c")
    cmd.append(str(path))
    run(cmd)
