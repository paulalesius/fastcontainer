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
from .log import setup_logger

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
@click.option('-p', '--profile', required=True,
              help="Profile name from the YAML 'profiles:' section (required).")
@click.option('-q', '--quiet', is_flag=True, help="Quiet mode: hide tool commands (only show progress + your RUN output).")
def main(containers_dir: Path, prepare_yaml: Path, profile: str, quiet: bool) -> None:
    """Build a container from a prepare.yaml using btrfs subvolumes + nspawn."""

    logger = setup_logger(quiet=quiet)

    # Root check
    if os.geteuid() != 0:
        logger.error("ERROR: This program must be run as root (use sudo)")
        sys.exit(1)

    # Single-build lock
    lock_path = containers_dir / ".fastcontainer.lock"
    lock_fd = None
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error(f"ERROR: Another fastcontainer build is already running in {containers_dir}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"ERROR: Failed to acquire lock: {e}")
        sys.exit(1)

    try:
        spec = BuildSpec.from_yaml(prepare_yaml)

        if profile not in spec.profiles:
            logger.error(f"ERROR: Profile '{profile}' not found in YAML. Available profiles: {list(spec.profiles.keys())}")
            sys.exit(1)

        selected_profile = spec.profiles[profile]

        builder = Builder(
            containers_dir=containers_dir,
            spec=spec,
            profile=selected_profile,
            quiet=quiet,
            logger=logger
        )
        builder.build()
    except Exception as e:
        logger.error(f"❌ Build failed: {e}")
        sys.exit(1)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass
