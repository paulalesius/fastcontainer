"""Shared fixtures and helpers for the fastcontainer test suite.

Everything here runs the REAL build pipeline (Builder + models) end-to-end,
with fakes injected at the two seams:

- DirBackstore      - directories instead of btrfs subvolumes
- RecordingExecutor - records nspawn/host calls instead of executing them

So the config processing (YAML, imports, env, profiles, steps, caching)
is exercised exactly as in a real build, without btrfs, nspawn or root.
"""
from pathlib import Path

import pytest

from fastcontainer.backstore import DirBackstore
from fastcontainer.builder import Builder
from fastcontainer.executor import RecordingExecutor
from fastcontainer.models import BuildSpec

HEX16 = r"[0-9a-f]{16}"
HEX40 = r"[0-9a-f]{40}"


def write_yaml(root: Path, text: str, name: str = "prepare.yaml") -> Path:
    """Write *text* as a YAML file under *root* and return its path."""
    p = root / name
    p.write_text(text, encoding="utf-8")
    return p


def do_build(store: Path, yaml_path: Path, profile: str, *,
             variables=None, prune=False, executor=None, backstore=None,
             post_cmd=None, shell=False, boot=False):
    """Run a full build of *profile* into *store* using fakes.

    Returns (backstore, executor, builder). Pass the same executor across
    calls to observe multi-build behaviour (cache hits, check gates, ...).
    """
    spec = BuildSpec.from_yaml(yaml_path, variables=variables or {})
    backstore = backstore or DirBackstore()
    executor = executor or RecordingExecutor()
    builder = Builder(
        containers_dir=store,
        spec=spec,
        profile=spec.profiles[profile],
        prune=prune,
        verbose=False,
        post_build_cmd=post_cmd,
        run_cmd=True,
        shell=shell,
        boot=boot,
        backstore=backstore,
        executor=executor,
    )
    builder.build()
    return backstore, executor, builder


def layer_names(store: Path, base_effective: str) -> list[str]:
    """Names of cached layer dirs __<base_effective>-<40hex> in *store*."""
    prefix = f"__{base_effective}-"
    out = []
    for p in sorted(store.iterdir()):
        if (p.is_dir() and p.name.startswith(prefix)
                and len(p.name) == len(prefix) + 40
                and all(c in "0123456789abcdef" for c in p.name[len(prefix):])):
            out.append(p.name)
    return out


def final_path(store: Path, base_effective: str, profile_name: str, fingerprint: str) -> Path:
    return store / f"{base_effective}-{profile_name}-{fingerprint}"


@pytest.fixture
def store_dir(tmp_path):
    """An empty container store (the builder's containers_dir)."""
    d = tmp_path / "store"
    d.mkdir()
    return d


# A realistic single-profile config: base with a create script, two steps
# (one with a per-step user), nspawn flags, and a check gate.
WEB_YAML = """\
base:
  name: testbase
  create: |
    echo "simulated debootstrap"
    mkdir -p /usr

profiles:
  web:
    add:
      - "--tmpfs=/var/tmp"
    steps:
      - RUN: |
          echo "layer one"
      - RUN(appuser): |
          echo "layer two as appuser"
    check: |
      test -f /etc/motd
"""
