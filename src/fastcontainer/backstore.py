"""Storage abstraction for the layered build.

The builder only needs three primitives: create an empty subvolume-like
object, snapshot one into another, and delete one.  Keeping them behind a
single interface lets the whole build pipeline run end-to-end in tests
(and with --dry-run) without btrfs or root:

- BtrfsBackstore - the real btrfs subvolume backend (requires root)
- DirBackstore   - plain-directory stand-in (no privileges needed)
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from .utils import run

logger = logging.getLogger("fastcontainer")


class Backstore(ABC):
    """Storage primitives for the layered build (subvolume create/snapshot/delete)."""

    @abstractmethod
    def create(self, path: Path) -> None:
        """Create a new empty storage object at *path*."""

    @abstractmethod
    def snapshot(self, src: Path, dst: Path) -> None:
        """Create a (cheap) snapshot copy of *src* at *dst*."""

    @abstractmethod
    def delete(self, path: Path) -> None:
        """Delete the storage object at *path* (with a safety check)."""

    @abstractmethod
    def is_subvolume(self, path: Path) -> bool:
        """Return True if *path* is a valid storage object of this backstore."""


class DirBackstore(Backstore):
    """Plain-directory stand-in for a btrfs subvolume store.

    - create()     -> mkdir -p
    - snapshot()   -> recursive copy (the snapshot gets its own independent
      tree, mirroring btrfs snapshot semantics closely enough for the
      manifest assertions in the tests)
    - delete()     -> refuses to remove a path that is not a directory
      (mirrors the btrfs safety check: never delete a plain file)
    - is_subvolume() -> is_dir()
    """

    def create(self, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def snapshot(self, src: Path, dst: Path) -> None:
        # copytree raises FileExistsError if dst exists, mirroring
        # "btrfs subvolume snapshot" failing on an existing target.
        shutil.copytree(Path(src), Path(dst))

    def delete(self, path: Path) -> None:
        path = Path(path)
        if not path.is_dir():
            logger.warning(f"Skipping delete: {path} is not a directory")
            return
        shutil.rmtree(path)

    def is_subvolume(self, path: Path) -> bool:
        return Path(path).is_dir()


class BtrfsBackstore(Backstore):
    """Real btrfs subvolume backend. Requires root."""

    def __init__(self) -> None:
        if os.geteuid() != 0:
            raise PermissionError("BtrfsBackstore requires root (use sudo)")

    def is_subvolume(self, path: Path) -> bool:
        """Return True if the path is a real btrfs subvolume."""
        path = Path(path)
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

    def snapshot(self, src: Path, dst: Path) -> None:
        """Create a btrfs snapshot (silent by default)."""
        run(["btrfs", "subvolume", "snapshot", str(src), str(dst)])

    def delete(self, path: Path) -> None:
        """Delete a btrfs subvolume with safety check."""
        path = Path(path).resolve()

        if not path.is_dir():
            logger.warning(f"Skipping delete: {path} is not a directory")
            return

        if not self.is_subvolume(path):
            raise RuntimeError(
                f"SAFETY: Refusing to delete {path} - "
                "it is not a btrfs subvolume."
            )

        run(["btrfs", "subvolume", "delete", "-c", str(path)])

    def create(self, path: Path) -> None:
        """Create a new empty btrfs subvolume."""
        run(["btrfs", "subvolume", "create", str(path)])
