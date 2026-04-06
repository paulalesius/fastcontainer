"""
fastcontainer CLI — build + exec
"""
import os
import sys
import fcntl
from pathlib import Path

import click

from .models import BuildSpec, Manifest
from .builder import Builder
from .nspawn import exec_in_container
from .log import setup_logger


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Minimal btrfs + systemd-nspawn layered container builder + runner."""
    pass


@main.command()
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
@click.option('--prune', is_flag=True, default=False, help="Prune intermediate layers after successful build (default: keep them for reuse).")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=False)
def build(containers_dir: Path, prepare_yaml: Path, profile: str, quiet: bool, prune: bool, command: tuple[str, ...]) -> None:
    """Build a container from a prepare.yaml using btrfs subvolumes + nspawn.

    Optional trailing command (after --) will be executed inside the final image:
        fastcontainer build ... -p run-llama -- /bin/bash -l
    """
    logger = setup_logger(quiet=quiet)

    if os.geteuid() != 0:
        logger.error("ERROR: This program must be run as root (use sudo)")
        sys.exit(1)

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
            if profile == "base":
                logger.error("ERROR: 'base' is a reserved special profile (common flags) and cannot be selected with -p base")
            else:
                logger.error(f"ERROR: Profile '{profile}' not found in YAML. Available profiles: {list(spec.profiles.keys())}")
            sys.exit(1)

        selected_profile = spec.profiles[profile]

        # CLI command overrides profile.cmd
        post_cmd = list(command) if command else None

        builder = Builder(
            containers_dir=containers_dir,
            spec=spec,
            profile=selected_profile,
            prune=prune,
            quiet=quiet,
            logger=logger,
            post_build_cmd=post_cmd,
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


@main.command()
@click.argument("containers_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path, resolve_path=True))
@click.argument("image", type=str)
@click.option('-q', '--quiet', is_flag=True, help="Quiet mode")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
def exec(containers_dir: Path, image: str, quiet: bool, command: tuple[str, ...]) -> None:
    """Run a command inside a built container (uses the exact nspawn profile from build time)."""
    logger = setup_logger(quiet=quiet)

    if os.geteuid() != 0:
        logger.error("ERROR: Must be run as root")
        sys.exit(1)

    container_path = containers_dir / image
    if not container_path.is_dir():
        logger.error(f"ERROR: Container '{image}' not found in {containers_dir}")
        sys.exit(1)

    manifest = Manifest.from_subvolume(container_path)

    logger.info(f"→ Running in {image}: {' '.join(command)}")

    exec_in_container(
        root=container_path,
        command=list(command),
        nspawn_template=manifest.nspawn_template,
        quiet=quiet
    )

    logger.info(f"✅ Command finished in {image}")


if __name__ == "__main__":
    main()
