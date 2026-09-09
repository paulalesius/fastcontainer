"""Concurrent-build coordination for one container store.

Multiple fastcontainer builds may run at the same time in the same
containers_dir. Correctness rests on three mechanisms, all built from
flock(2) on small files, and on one btrfs property:

- btrfs snapshots are CoW. Once a build has snapshotted a layer or image,
  its data is self-contained: deleting the source is harmless to the
  snapshot's owner. So there is no "who is using this subvolume" to detect;
  coordination only ever needs to happen on *path names*, never on content.

- StoreLocks: one flock file per store resource (base, layer, final image).
  A build computes its whole resource plan up front (layer names are
  content-addressed and fully determinable before executing anything) and
  acquires all of its locks in sorted order. A single global acquisition
  order makes deadlock impossible.

- BuildLiveness: each build holds an exclusive flock on its own
  'live-<uuid>' file for its whole lifetime and publishes the temp
  subvolumes it currently owns in a 'claims-<uuid>' file (rewritten
  atomically). The kernel releases a flock when the process dies, so a
  claim is only trusted while its live file is locked. That is exactly how
  stale-temp cleanup tells a crashed build's orphan from a live build's
  work in progress — no timeouts, no mtime heuristics.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import uuid
from pathlib import Path

logger = logging.getLogger("fastcontainer")

LOCK_DIR = ".fastcontainer-locks"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _lock_basename(resource: str) -> str:
    """Map a store resource name to a lock-file stem.

    Store names are validated single path segments (see
    models._validate_container_name), so for them this is the identity and
    distinct names always get distinct lock files. The substitution is belt
    & braces for any future caller passing something else; two exotic names
    that collide only over-serialize, which is safe.
    """
    return _UNSAFE.sub("_", resource) or "unnamed"


class StoreLocks:
    """Per-resource flock coordination for one container store.

    Lock files are created once and NEVER removed: removing a lock file
    while another process may still be blocked on its old inode lets a
    third process create and lock a *different* file, so two builds could
    hold the "same" lock at once. Stale lock files are harmless (flock is
    held per open file description and the next build simply locks the same
    file again), so we keep them.
    """

    def __init__(self, containers_dir: Path) -> None:
        self.lock_dir = Path(containers_dir) / LOCK_DIR
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._fds: dict[str, int] = {}   # lock-file stem -> open fd

    def _path(self, resource: str) -> Path:
        return self.lock_dir / f"{_lock_basename(resource)}.lock"

    def acquire(self, resources) -> list[str]:
        """Lock every *resource* (sorted, blocking).

        Returns the resources this call actually took. Names already held
        by this StoreLocks instance are skipped, which is what makes a
        nested parent-profile build re-entrant: the outer (leaf) build
        already locked the whole extend-chain, so the parent build acquires
        nothing new and can never deadlock against itself.
        """
        taken: list[str] = []
        for resource in sorted(set(resources)):
            stem = _lock_basename(resource)
            if stem in self._fds:
                continue
            fd = os.open(self._path(resource), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.info(
                    f"Waiting for lock on '{resource}' held by another build..."
                )
                fcntl.flock(fd, fcntl.LOCK_EX)
            self._fds[stem] = fd
            taken.append(resource)
        return taken

    def release(self, resources) -> None:
        """Release the given resources (idempotent)."""
        for resource in set(resources):
            fd = self._fds.pop(_lock_basename(resource), None)
            if fd is None:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def release_all(self) -> None:
        self.release(list(self._fds))


class BuildLiveness:
    """Liveness + temp-ownership record for one build (or nested parent build).

    - The live file is exclusively flocked for the build's lifetime. The
      kernel drops the lock when the process dies, so the answer to "is the
      owner of this liveness file still running" is: "can I still flock it
      non-blockingly?"
    - The claims file lists the temp subvolumes this build currently owns.
      It is rewritten via write-to-temp + rename(2) (atomic on the same
      filesystem), so a concurrent reader always sees a consistent
      snapshot. The claim is written BEFORE the temp subvolume is created,
      which closes the crash window: a temp that exists is always either
      claimed by a live build or orphaned by a dead one.
    """

    def __init__(self, lock_dir: Path) -> None:
        self._dir = Path(lock_dir)
        self._id = uuid.uuid4().hex
        self._live = self._dir / f"live-{self._id}"
        self._claims = self._dir / f"claims-{self._id}"
        self._claims_set: set[str] = set()
        self._fd = os.open(self._live, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # we just created it
        self._write_claims()

    def _write_claims(self) -> None:
        tmp = self._claims.with_name(self._claims.name + f".tmp-{uuid.uuid4().hex}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(self._claims_set)) + ("\n" if self._claims_set else ""))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._claims)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    def claim(self, name: str) -> None:
        self._claims_set.add(name)
        self._write_claims()

    def unclaim(self, name: str) -> None:
        self._claims_set.discard(name)
        self._write_claims()

    def close(self) -> None:
        """Drop our flock and remove our own files (safe: we released first)."""
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
        for p in (self._live, self._claims):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    # -- cross-build queries --------------------------------------------------

    @staticmethod
    def _holder_alive(live: Path) -> bool:
        """True if some process currently holds the flock on *live*."""
        try:
            fd = os.open(live, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)   # closing releases the probe lock
        return False

    @staticmethod
    def _remove_pair(live: Path) -> None:
        for p in (live, live.parent / live.name.replace("live-", "claims-", 1)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def sweep_dead(self) -> int:
        """Remove live/claims files of builds that are gone (flock released).

        A dead build's uuid is never reused, so unlinking its files is safe
        once the probe flock was acquired: nobody live can hold them.
        Returns the number of dead builds cleaned up.
        """
        removed = 0
        for live in sorted(self._dir.glob("live-*")):
            if live == self._live:
                continue
            if not self._holder_alive(live):
                self._remove_pair(live)
                removed += 1
        return removed

    def is_protected(self, name: str) -> bool:
        """True if a build that is still alive owns the temp subvolume *name*.

        A claim from a dead build (its flock is gone) is reclaimed here and
        does NOT protect: the owner can no longer need the temp.
        """
        for claims in sorted(self._dir.glob("claims-*")):
            if claims == self._claims:
                continue
            try:
                lines = claims.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            if name not in lines:
                continue
            live = claims.parent / claims.name.replace("claims-", "live-", 1)
            if not live.exists() or not self._holder_alive(live):
                # Dead claimant (or partially swept): reclaim, temp is stale.
                self._remove_pair(live)
                continue
            return True
        return False
