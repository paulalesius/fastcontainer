from pathlib import Path

from .utils import run


def snapshot(src: Path | str, dest: Path | str, quiet: bool = False) -> None:
    """Create a btrfs subvolume snapshot."""
    run(
        ["btrfs", "subvolume", "snapshot", str(src), str(dest)],
        quiet=quiet,
        capture_output=quiet,   # ← this silences the "Create snapshot of..." messages
    )


def delete(path: Path | str, commit: bool = True, quiet: bool = False) -> None:
    """Delete a btrfs subvolume (with optional commit)."""
    cmd = ["btrfs", "subvolume", "delete"]
    if commit:
        cmd.append("-c")
    cmd.append(str(path))
    run(
        cmd,
        quiet=quiet,
        capture_output=quiet,   # ← this silences the "Delete subvolume..." messages
    )
