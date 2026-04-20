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
from contextlib import contextmanager

@contextmanager
def acquire_build_lock(containers_dir: Path):
    """Exclusive lock so only one build runs at a time."""
    lock_path = containers_dir / ".fastcontainer.lock"
    lock_fd = None
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock_fd
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass

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
@click.option('-v', '--verbose', is_flag=True,
              help="Verbose mode: show full output of each build step and internal commands (default: clean progress only).")
@click.option('--prune', is_flag=True, default=False, help="Prune intermediate layers after successful build.")
@click.option('-D', '--define', 'defines', multiple=True, metavar='KEY=VALUE',
              help='Define a variable KEY=VALUE for use inside add: flags (repeatable).')
@click.option('-s', '--shell-on-fail', 'shell_on_fail', is_flag=True,
              help="Drop into an interactive shell inside the failed container state if a build step fails.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=False)
def build(containers_dir: Path, prepare_yaml: Path, profile: str, verbose: bool, prune: bool, defines: tuple[str, ...] = (), shell_on_fail: bool = False, command: tuple[str, ...] = ()) -> None:
    """Build a container from a prepare.yaml using btrfs subvolumes + nspawn.

    Optional trailing command (after --) will be executed inside the final image.
    """
    logger = setup_logger(verbose=verbose)

    if os.geteuid() != 0:
        logger.error("ERROR: This program must be run as root (use sudo)")
        sys.exit(1)

    variables: dict[str, str] = {}
    for d in defines:
        if '=' not in d:
            logger.error(f"ERROR: Invalid -D flag: '{d}'. Use the format KEY=VALUE")
            sys.exit(1)
        key, value = d.split('=', 1)
        key = key.strip()
        if not key or not key.isidentifier():
            logger.error(f"ERROR: Invalid variable name in -D: '{key}' (must be a valid identifier)")
            sys.exit(1)
        variables[key] = value.strip()

    # === Exclusive lock for the entire build ===
    try:
        with acquire_build_lock(containers_dir):
            spec = BuildSpec.from_yaml(prepare_yaml, variables=variables)

            if profile not in spec.profiles:
                if profile == "base":
                    logger.error("ERROR: 'base' is a reserved special profile and cannot be selected")
                else:
                    logger.error(f"ERROR: Profile '{profile}' not found. Available: {list(spec.profiles.keys())}")
                sys.exit(1)

            selected_profile = spec.profiles[profile]

            post_cmd = list(command) if command else None

            builder = Builder(
                containers_dir=containers_dir,
                spec=spec,
                profile=selected_profile,
                prune=prune,
                verbose=verbose,
                logger=logger,
                post_build_cmd=post_cmd,
                run_cmd=True,
                shell_on_fail=shell_on_fail,
            )
            builder.build()
    except BlockingIOError:
        logger.error(f"ERROR: Another fastcontainer build is already running in {containers_dir}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"ERROR: Build failed: {e}")
        sys.exit(1)

@main.command()
@click.argument("containers_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path, resolve_path=True))
@click.argument("image", type=str)
@click.option('-v', '--verbose', is_flag=True, help="Verbose mode")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
def exec(containers_dir: Path, image: str, verbose: bool, command: tuple[str, ...]) -> None:
    """Run a command inside a built container."""
    logger = setup_logger(verbose=verbose)

    if os.geteuid() != 0:
        logger.error("ERROR: Must be run as root")
        sys.exit(1)

    container_path = containers_dir / image
    if not container_path.is_dir():
        logger.error(f"ERROR: Container '{image}' not found in {containers_dir}")
        sys.exit(1)

    manifest = Manifest.from_subvolume(container_path)

    logger.info(f"Running in {image}: {' '.join(command)}")

    exec_in_container(
        root=container_path,
        command=list(command),
        nspawn_template=manifest.nspawn_template,
    )

    logger.info(f"Command finished in {image}")


if __name__ == "__main__":
    main()
