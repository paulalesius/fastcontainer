"""CLI tests: per-resource lock behaviour and click-level validation (CliRunner).

Builds no longer take a single global lock; each build locks the store paths
it will touch (base, layers, final images) via StoreLocks, and records its
live temp subvolumes via BuildLiveness. These tests cover the new primitives
plus the click-level validation. The root check was moved out of the CLI
into the real backend constructors (BtrfsBackstore / NspawnExecutor), so
everything here runs unprivileged.
"""
import fcntl
import os
import threading

from click.testing import CliRunner

import pytest

from fastcontainer.cli import build
from fastcontainer.locks import BuildLiveness, StoreLocks

from conftest import WEB_YAML, write_yaml


class TestStoreLocks:
    def test_second_holder_on_same_resource_is_blocked(self, tmp_path):
        locks = StoreLocks(tmp_path)
        locks.acquire(["alpha"])
        # A *different* lock instance (like a different build) must not get
        # the same resource: a non-blocking probe flock must fail.
        fd = os.open(locks.lock_dir / "alpha.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_lock_is_released_and_reacquirable(self, tmp_path):
        locks = StoreLocks(tmp_path)
        locks.acquire(["alpha", "beta"])
        locks.release_all()
        # Released resources are immediately re-acquirable by another build,
        # and the lock files themselves stay in place (never unlinked).
        again = StoreLocks(tmp_path)
        assert again.acquire(["alpha", "beta"]) == ["alpha", "beta"]
        assert (tmp_path / ".fastcontainer-locks" / "alpha.lock").exists()

    def test_reacquiring_own_lock_is_a_noop(self, tmp_path):
        # Nested parent-profile builds share one StoreLocks instance: the
        # parent build's plan is a subset of the outer plan, so it must not
        # block against itself.
        locks = StoreLocks(tmp_path)
        locks.acquire(["a", "b", "c"])
        assert locks.acquire(["b"]) == []
        assert locks.acquire(["b", "a", "x"]) == ["x"]
        locks.release_all()

    def test_overlapping_sets_in_reverse_order_do_not_deadlock(self, tmp_path):
        # Two builds whose lock sets overlap in different orders. Sorted
        # acquisition gives a total order, so this must never deadlock.
        locks_a, locks_b = StoreLocks(tmp_path), StoreLocks(tmp_path)
        results = {}

        def build_a():
            locks_a.acquire(["res-a", "res-shared", "res-c"])
            results["a"] = True
            locks_a.release_all()

        def build_b():
            locks_b.acquire(["res-c", "res-shared", "res-d"])
            results["b"] = True
            locks_b.release_all()

        ta, tb = threading.Thread(target=build_a), threading.Thread(target=build_b)
        ta.start(); tb.start()
        ta.join(timeout=10); tb.join(timeout=10)
        assert not ta.is_alive() and not tb.is_alive(), "deadlock: sorted acquisition failed"
        assert results == {"a": True, "b": True}

    def test_lock_files_are_never_removed(self, tmp_path):
        locks = StoreLocks(tmp_path)
        taken = locks.acquire(["alpha"])
        assert (tmp_path / ".fastcontainer-locks" / "alpha.lock").exists()
        locks.release(taken)
        # After release the file stays: a blocked contender creating or
        # unlinking the lock file is what allows two builds to hold the
        # "same" lock (on different inodes) at once.
        assert (tmp_path / ".fastcontainer-locks" / "alpha.lock").exists()

    def test_distinct_resources_get_distinct_lock_files(self, tmp_path):
        locks = StoreLocks(tmp_path)
        locks.acquire(["__testbase-" + "a" * 40, "testbase-web-" + "b" * 40])
        d = tmp_path / ".fastcontainer-locks"
        assert len(list(d.glob("*.lock"))) == 2


class TestBuildLiveness:
    def _lockdir(self, tmp_path):
        d = tmp_path / ".fastcontainer-locks"
        d.mkdir(exist_ok=True)
        return d

    @staticmethod
    def _crash(live: BuildLiveness) -> None:
        """Simulate SIGKILL: release the flock and drop the fd, but leave the
        live/claims files on disk (a crash cannot run close())."""
        fcntl.flock(live._fd, fcntl.LOCK_UN)
        os.close(live._fd)

    def test_live_claim_protects_temp_from_other_builds(self, tmp_path):
        a = BuildLiveness(self._lockdir(tmp_path))
        b = BuildLiveness(self._lockdir(tmp_path))
        a.claim("_testbase-temp-aaaa")
        assert b.is_protected("_testbase-temp-aaaa") is True
        assert b.is_protected("_testbase-temp-unknown") is False
        a.close(); b.close()

    def test_dead_claim_does_not_protect_and_is_reclaimed(self, tmp_path):
        a = BuildLiveness(self._lockdir(tmp_path))
        a.claim("_testbase-temp-aaaa")
        a_live, a_claims = a._live, a._claims
        self._crash(a)   # process dies: kernel drops the flock, files remain
        b = BuildLiveness(self._lockdir(tmp_path))
        # The dead build is swept, and its claim does NOT protect: the
        # orphaned temp is fair game for cleanup.
        assert b.sweep_dead() == 1
        assert b.is_protected("_testbase-temp-aaaa") is False
        assert not a_live.exists() and not a_claims.exists()
        b.close()

    def test_sweep_dead_removes_only_dead_builds(self, tmp_path):
        a = BuildLiveness(self._lockdir(tmp_path))
        b = BuildLiveness(self._lockdir(tmp_path))
        c = BuildLiveness(self._lockdir(tmp_path))
        self._crash(a)   # a dies, b and c stay alive
        removed = c.sweep_dead()
        assert removed == 1
        # b's liveness files must still be there
        assert (tmp_path / ".fastcontainer-locks").glob("live-*")
        b.close(); c.close()


BASE_YAML = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo hi
"""


class TestCliValidation:
    def setup_method(self):
        self.runner = CliRunner()

    def invoke(self, *args, tmp_path):
        # click needs plain strings, not PosixPath objects
        return self.runner.invoke(build, [str(a) for a in args], obj=None)

    def test_build_help(self, tmp_path):
        r = self.runner.invoke(build, ["--help"])
        assert r.exit_code == 0
        assert "--dry-run" in r.output
        assert "-D" in r.output

    def test_invalid_dash_d_without_equals(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "p", "-D", "NOEQUALS", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "Invalid -D flag" in r.output

    def test_invalid_dash_d_key(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "p", "-D", "BAD KEY=1", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "Invalid variable name in -D" in r.output

    def test_profile_not_found_lists_available(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "nope", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "Profile 'nope' not found" in r.output
        assert "'p'" in r.output

    def test_base_is_reserved_profile(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "base", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "reserved special profile" in r.output

    def test_invalid_yaml_errors_cleanly(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, "base: \"\"\n")
        r = self.invoke(d, y, "-p", "p", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "ERROR:" in r.output
        assert "base" in r.output

    def test_dry_run_builds_full_pipeline_without_touching_store(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, WEB_YAML)
        r = self.invoke(d, y, "-p", "web", "--dry-run", tmp_path=tmp_path)
        assert r.exit_code == 0, r.output
        assert "DRY RUN OK" in r.output
        # the real container store must be completely untouched (locks and
        # liveness files live in the private dry-run workdir, not in the store)
        assert list(d.iterdir()) == []

    def test_dry_run_catches_config_errors_before_simulating(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, "import-base: ghost.yaml\n")
        r = self.invoke(d, y, "-p", "p", "--dry-run", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "ERROR:" in r.output
        assert list(d.iterdir()) == []
