"""End-to-end concurrency tests for the per-resource lock design.

Two properties are under test:

1. Overlapping builds (same store, overlapping or identical lock plans)
   never deadlock (sorted up-front acquisition) and never corrupt the
   store (every path is created/deleted only under its own lock).

2. Temp subvolumes of a LIVE build are never swept by another build's
   stale-temp cleanup, while temps of a CRASHED build (flock already
   dropped by the kernel) are swept again — via BuildLiveness claims.

Everything runs on the fake backends (DirBackstore / RecordingExecutor),
but with the REAL locking primitives (plain-file flocks), so these tests
exercise exactly the coordination a real multi-build store gets.
"""
import fcntl
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

from fastcontainer.backstore import DirBackstore
from fastcontainer.executor import RecordingExecutor
from fastcontainer.models import BuildSpec
from fastcontainer.builder import Builder, plan_build_resources
from fastcontainer.locks import LOCK_DIR, StoreLocks, _lock_basename

from conftest import WEB_YAML, do_build, write_yaml

OVERLAP_YAML = """\
base:
  name: testbase
  create: "echo base"
profiles:
  web:
    steps:
      - RUN: echo "layer one"
      - RUN(appuser): echo "layer two"
  api:
    steps:
      - RUN: echo "api layer"
"""

EXTEND_YAML = """\
base:
  name: testbase
  create: "echo base"
profiles:
  base-prof:
    steps:
      - RUN: echo "parent step"
    check: "test -f /nonexistent"
  child:
    extend: base-prof
    steps:
      - RUN: echo "child step"
"""


def _run_build(store, yaml_path, profile, executor):
    """Build *profile* into *store* with the given executor (thread body)."""
    spec = BuildSpec.from_yaml(yaml_path)
    builder = Builder(
        containers_dir=store,
        spec=spec,
        profile=spec.profiles[profile],
        backstore=DirBackstore(),
        executor=executor,
    )
    builder.build()


def _no_leftover_temps(store: Path) -> None:
    temps = [
        p.name for p in store.iterdir()
        if p.name.startswith("_") and not p.name.startswith("__")
    ]
    assert temps == [], f"leftover temp subvolumes: {temps}"


def _no_stale_liveness(store: Path) -> None:
    live = list((store / ".fastcontainer-locks").glob("live-*"))
    assert live == [], f"stale liveness files: {[p.name for p in live]}"


# ─────────────────────────────────────────────────────────────────────────────
# 1 — overlapping concurrent builds
# ─────────────────────────────────────────────────────────────────────────────

def test_two_concurrent_builds_same_profile(tmp_path, store_dir):
    """Cold store, same profile twice: exactly one base creation, no
    deadlock, no corruption, no leftovers."""
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    exec_a, exec_b = RecordingExecutor(), RecordingExecutor()
    t1 = threading.Thread(target=_run_build, args=(store_dir, yaml_path, "web", exec_a))
    t2 = threading.Thread(target=_run_build, args=(store_dir, yaml_path, "web", exec_b))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "deadlock in same-profile builds"

    spec = BuildSpec.from_yaml(yaml_path)
    prof = spec.profiles["web"]
    final = store_dir / spec.base.effective_name / ""  # noqa - readability
    final = store_dir / f"{spec.base.effective_name}-{prof.name}-{prof.fingerprint}"
    assert final.is_dir()
    _no_leftover_temps(store_dir)
    _no_stale_liveness(store_dir)
    # The second build re-checked the base under its lock and found it:
    # the create script ran exactly once in total.
    create_calls = sum(1 for e in (exec_a, exec_b) for c in e.calls if c[0] == "create_base")
    assert create_calls == 1


def test_concurrent_builds_overlapping_profiles_share_base(tmp_path, store_dir):
    """Two different profiles of the same base, cold store: the base is
    created once, both finals exist, the layer cache is intact."""
    yaml_path = write_yaml(tmp_path, OVERLAP_YAML)
    exec_a, exec_b = RecordingExecutor(), RecordingExecutor()
    t1 = threading.Thread(target=_run_build, args=(store_dir, yaml_path, "web", exec_a))
    t2 = threading.Thread(target=_run_build, args=(store_dir, yaml_path, "api", exec_b))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "deadlock in overlapping builds"

    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    assert (store_dir / eff).is_dir()  # base
    for prof_name in ("web", "api"):
        prof = spec.profiles[prof_name]
        final = store_dir / f"{eff}-{prof_name}-{prof.fingerprint}"
        assert final.is_dir(), f"missing final {final.name}"
        # final carries its manifest
        assert (final / "fastcontainer.json").is_file()
    _no_leftover_temps(store_dir)
    _no_stale_liveness(store_dir)
    create_calls = sum(1 for e in (exec_a, exec_b) for c in e.calls if c[0] == "create_base")
    assert create_calls == 1


def test_concurrent_deep_rebuild_and_dependent_build(tmp_path, store_dir):
    """A check: gate fails for profile 'base-prof' (deep rebuild) while a
    build of the dependent 'child' runs at the same time. The lock plans
    overlap (child's plan is a superset of the parent's) — no deadlock, and
    both finals must end up existing and consistent."""
    yaml_path = write_yaml(tmp_path, EXTEND_YAML)
    exec_a, exec_b = RecordingExecutor(check_result=False), RecordingExecutor(check_result=False)
    t1 = threading.Thread(target=_run_build, args=(store_dir, yaml_path, "base-prof", exec_a))
    t2 = threading.Thread(target=_run_build, args=(store_dir, yaml_path, "child", exec_b))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "deadlock in deep-rebuild race"

    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    for prof_name in ("base-prof", "child"):
        prof = spec.profiles[prof_name]
        final = store_dir / f"{eff}-{prof_name}-{prof.fingerprint}"
        assert final.is_dir(), f"missing final {final.name}"
        assert (final / "fastcontainer.json").is_file()
    _no_leftover_temps(store_dir)
    _no_stale_liveness(store_dir)


# ─────────────────────────────────────────────────────────────────────────────
# 2 — liveness: live temps are kept, dead temps are swept
# ─────────────────────────────────────────────────────────────────────────────

def _hold_claim(lock_dir: str, temp_name: str, store: str, q):
    """Child process: claim *temp_name*, create it, then hold the flock
    until killed. (Module-level so it can be spawned.)"""
    from fastcontainer.locks import BuildLiveness
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    live = BuildLiveness(Path(lock_dir))
    live.claim(temp_name)
    (Path(store) / temp_name).mkdir(parents=True, exist_ok=True)
    q.put("ready")
    time.sleep(60)   # hold until killed


def _spawn_claimant(store: Path, temp_name: str):
    q = mp.Queue()
    p = mp.Process(
        target=_hold_claim,
        args=(str(store / ".fastcontainer-locks"), temp_name, str(store), q),
    )
    p.start()
    assert q.get(timeout=15) == "ready"
    return p


def test_live_build_temp_is_kept_by_other_builds(tmp_path, store_dir):
    """Another build's cleanup must not delete a temp that a live build
    (its flock is held by a real second process) claims."""
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    eff = BuildSpec.from_yaml(yaml_path).base.effective_name
    temp_name = f"_{eff}-temp-" + "f" * 40

    p = _spawn_claimant(store_dir, temp_name)
    try:
        do_build(store_dir, yaml_path, "web")
        assert (store_dir / temp_name).is_dir(), "live build's temp was swept"
    finally:
        p.kill()
        p.join()
    # After the claimant died, its liveness files are stale; a further
    # build reclaims the dead claim and sweeps the orphaned temp.
    import shutil
    spec = BuildSpec.from_yaml(yaml_path)
    prof = spec.profiles["web"]
    final = store_dir / f"{eff}-{prof.name}-{prof.fingerprint}"
    if final.is_dir():
        shutil.rmtree(final)  # force a real delta build (a check: pass would skip cleanup)
    do_build(store_dir, yaml_path, "web")
    assert not (store_dir / temp_name).exists()
    _no_stale_liveness(store_dir)


def test_crashed_build_temp_is_swept(tmp_path, store_dir):
    """A build that dies (flock released by the kernel) leaves a claim and
    a temp behind; the next build must reclaim the dead claim and sweep the
    temp."""
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    eff = BuildSpec.from_yaml(yaml_path).base.effective_name
    temp_name = f"_{eff}-temp-" + "f" * 40

    p = _spawn_claimant(store_dir, temp_name)
    p.kill()       # simulate a crash: liveness + temp remain, flock is gone
    p.join()
    assert (store_dir / temp_name).is_dir()  # orphan still on disk

    do_build(store_dir, yaml_path, "web")
    assert not (store_dir / temp_name).exists(), "crashed build's temp was not swept"
    _no_stale_liveness(store_dir)


def test_crash_during_step_then_rebuild_succeeds(tmp_path, store_dir):
    """Simulate a build killed mid-step: base exists, a layer temp is left
    behind (no liveness record at all — the crash predates the record, or
    it was already swept), no final yet. The next build must sweep the
    temp and complete successfully."""
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name

    (store_dir / eff).mkdir()  # base already built by an earlier build
    # crash leftovers from an interrupted 'web' build
    orphan_layer_temp = store_dir / (f"_{eff}-temp-" + "e" * 40)
    orphan_layer_temp.mkdir()
    orphan_base_temp = store_dir / (f"_{spec.base.name}-create-" + "d" * 40)
    orphan_base_temp.mkdir()
    orphan_final_temp = store_dir / (f"_{eff}-final-" + "c" * 40)
    orphan_final_temp.mkdir()

    do_build(store_dir, yaml_path, "web")

    prof = spec.profiles["web"]
    final = store_dir / f"{eff}-{prof.name}-{prof.fingerprint}"
    assert final.is_dir()
    assert (final / "fastcontainer.json").is_file()
    for orphan in (orphan_layer_temp, orphan_base_temp, orphan_final_temp):
        assert not orphan.exists(), f"orphan {orphan.name} was not swept"
    _no_leftover_temps(store_dir)
    _no_stale_liveness(store_dir)


def test_plan_covers_every_path_the_build_writes_or_deletes(tmp_path, store_dir):
    """The up-front lock plan must enumerate every store path the pipeline
    can create or delete. A path missing from the plan would be written
    while unlocked — exactly the hole the old global lock closed. Verified
    on a fresh store with --prune: every layer the build touched (created
    or cache-hit) plus the final image must all have been in the plan."""
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    prof = spec.profiles["web"]
    plan = set(plan_build_resources(spec, prof))
    assert eff in plan, "base must be locked"
    assert prof.fingerprint and f"{eff}-{prof.name}-{prof.fingerprint}" in plan

    b = Builder(store_dir, spec, prof, prune=True,
                backstore=DirBackstore(), executor=RecordingExecutor())
    b.build()

    assert b.final_path.is_dir()
    assert b.final_name in plan, "final image was written without its lock"
    for layer in sorted(b._layers_touched):
        assert layer.name in plan, f"layer {layer.name} was written without its lock"
    # Prune (also under the locks) removed every touched layer; the final
    # image, being a self-contained snapshot, survived.
    for layer in sorted(b._layers_touched):
        assert not layer.is_dir(), f"prune left {layer.name} behind"
    assert b.final_path.is_dir()


class TestDeleteVanishedPath:
    """The stale-temp cleanup scans the store and then deletes each stale
    temp. Under concurrency the owner may finish (rename the temp into its
    final image) between the scan and the delete. A vanished path must be a
    silent no-op — not an exception, not a scary warning — while a plain
    file squatting where a subvolume belongs is still a safety event."""

    def test_delete_nonexistent_path_is_silent_noop(self, tmp_path, caplog):
        import logging
        bs = DirBackstore()
        gone = tmp_path / "gone"
        with caplog.at_level(logging.WARNING, logger="fastcontainer"):
            bs.delete(gone)
        assert not gone.exists()
        assert not [r for r in caplog.records if "Skipping delete" in r.message]

    def test_delete_plain_file_still_warns_and_keeps_file(self, tmp_path, caplog):
        import logging
        bs = DirBackstore()
        f = tmp_path / "plain"
        f.write_text("not a subvolume")
        with caplog.at_level(logging.WARNING, logger="fastcontainer"):
            bs.delete(f)
        assert f.exists()
        assert any("Skipping delete" in r.message for r in caplog.records)


class _FailingExecutor(RecordingExecutor):
    """Every build-step execution fails: build() must raise, release all
    its locks, and leave the store ready for the next build."""

    def execute(self, root, cmd, nspawn, user="root", verbose=False):
        raise RuntimeError("simulated step failure")


def test_failed_build_releases_locks_and_leaves_no_trace(tmp_path, store_dir):
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    prof = spec.profiles["web"]
    plan = sorted(set(plan_build_resources(spec, prof)))
    locks = StoreLocks(store_dir)

    b = Builder(store_dir, spec, prof,
                backstore=DirBackstore(), executor=_FailingExecutor(), locks=locks)
    with pytest.raises(RuntimeError):
        b.build()

    # Every lock the failed build took is released again: a fresh probe can
    # take each one non-blockingly. (If one were still held this HANGS,
    # which is the failure mode the test is looking for.)
    for resource in plan:
        fd = os.open(store_dir / LOCK_DIR / f"{_lock_basename(resource)}.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # No half-built artifacts: the failed step's temp was released in
    # _build_layer's finally, and the failed build's own liveness files
    # were removed on the way out.
    assert not b.final_path.is_dir()
    _no_leftover_temps(store_dir)
    _no_stale_liveness(store_dir)

    # And the next build on the same store succeeds (the full plan is
    # acquirable again end to end).
    do_build(store_dir, yaml_path, "web")
    assert b.final_path.is_dir()
    assert (b.final_path / "fastcontainer.json").is_file()
    _no_leftover_temps(store_dir)
    _no_stale_liveness(store_dir)
