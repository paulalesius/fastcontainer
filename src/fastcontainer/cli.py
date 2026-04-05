# fastcontainer/fastcontainer/cli.py
#!/usr/bin/env python3
"""
fastcontainer CLI — built with Click
"""
import os
import sys
import fcntl
from pathlib import Path

import click

from .models import BuildSpec
from .builder import Builder


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Minimal btrfs + systemd-nspawn layered container builder.",
)
@click.argument(
    "containers_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path, resolve_path=True),
)
@click.argument(
    "prepare_yaml",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path, resolve_path=True),
)
def main(containers_dir: Path, prepare_yaml: Path) -> None:
    """Build a container from a prepare.yaml using btrfs subvolumes + nspawn."""

    # Root check (exact same message as before)
    if os.geteuid() != 0:
        click.secho("ERROR: This program must be run as root (use sudo)", fg="red")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # Single-build lock (only one fastcontainer can run at a time)
    # ─────────────────────────────────────────────────────────────
    lock_path = containers_dir / ".fastcontainer.lock"
    lock_fd = None
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        click.secho(
            f"ERROR: Another fastcontainer build is already running in {containers_dir}",
            fg="red",
        )
        sys.exit(1)
    except Exception as e:
        click.secho(f"ERROR: Failed to acquire lock: {e}", fg="red")
        sys.exit(1)

    try:
        spec = BuildSpec.from_yaml(prepare_yaml)
        # Safety checks (exact same as original)
        base_path = containers_dir / spec.base
        if not base_path.is_dir():
            click.secho(f"ERROR: Base subvolume not found: {base_path}", fg="red")
            sys.exit(1)

        if not base_path.resolve().is_relative_to(containers_dir):
            click.secho("ERROR: Base subvolume must stay inside the containers directory", fg="red")
            sys.exit(1)

        builder = Builder(containers_dir=containers_dir, spec=spec)
        builder.build()
    except Exception as e:
        click.secho(f"❌ Build failed: {e}", fg="red")
        sys.exit(1)
    finally:
        # Always release the lock cleanly
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass  # best-effort cleanup
