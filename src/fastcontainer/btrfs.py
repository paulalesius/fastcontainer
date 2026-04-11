from pathlib import Path
import subprocess
import logging

from .utils import run

logger = logging.getLogger("fastcontainer")


def is_btrfs_subvolume(path: Path | str) -> bool:
    """Return True if the path is a real btrfs subvolume."""
    try:
        result = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def snapshot(src: Path | str, dest: Path | str) -> None:
    """Create a btrfs snapshot (silent by default)."""
    run(["btrfs", "subvolume", "snapshot", str(src), str(dest)])


def delete(path: Path | str, commit: bool = True) -> None:
    """Delete a btrfs subvolume with safety check."""
    path = Path(path).resolve()

    if not path.is_dir():
        logger.warning(f"Skipping delete: {path} is not a directory")
        return

    if not is_btrfs_subvolume(path):
        raise RuntimeError(
            f"SAFETY: Refusing to delete {path} — "
            "it is not a btrfs subvolume."
        )

    cmd = ["btrfs", "subvolume", "delete"]
    if commit:
        cmd.append("-c")
    cmd.append(str(path))
    run(cmd)


def create(path: Path | str) -> None:
    """Create a new empty btrfs subvolume."""
    run(["btrfs", "subvolume", "create", str(path)])
