"""
fastcontainer CLI — build
"""
import fcntl
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import click

from .backstore import DirBackstore
from .builder import Builder
from .executor import NspawnExecutor, RecordingExecutor
from .log import setup_logger
from .models import BuildSpec

@contextmanager
def acquire_build_lock(containers_dir: Path):
    """Exclusive lock so only one build runs at a time.

    The lock is a flock on a long-lived file and the file is *never removed*:
    if a blocked process unlinked it, a third process could create and lock a
    different file while the original holder still had its lock, so two builds
    could run concurrently in the same store. A stale lock file is harmless -
    flock is held per open file description, and the next build simply locks
    the same file again.
    """
    lock_path = containers_dir / ".fastcontainer.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock_fd
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Minimal btrfs + systemd-nspawn layered container builder."""
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
@click.option('--prune', is_flag=True, default=False, help="Prune the intermediate layers used by this build after a successful build.")
@click.option('-D', '--define', 'defines', multiple=True, metavar='KEY=VALUE',
              help='Define a variable KEY=VALUE for use inside add: flags (repeatable).')
@click.option('-s', '--shell', 'shell', is_flag=True,
              help="Drop into an interactive shell: on build failure (in the failed temporary layer) OR on success (in the final image instead of running cmd or trailing command).")
@click.option('-b', '--boot', 'boot', is_flag=True,
              help="Boot mode: run the final cmd: (or interactive shell with -s) using systemd-nspawn --boot (in addition to --ephemeral). "
                   "This starts the container as a full machine (init/PID 1). ...")
@click.option('--dry-run', is_flag=True, default=False,
              help="Simulate the full build (config parsing, env expansion, profile inheritance, layer plan) "
                   "without root, btrfs or systemd-nspawn. Nothing is written to the real container store.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=False)
def build(containers_dir: Path, prepare_yaml: Path, profile: str, verbose: bool, prune: bool,
          defines: tuple[str, ...] = (), shell: bool = False, boot: bool = False,
          dry_run: bool = False, command: tuple[str, ...] = ()) -> None:
    """Build a container from a prepare.yaml using btrfs subvolumes + nspawn.

    Optional trailing command (after --) will be executed inside the final image.
    With --shell the trailing command is ignored and you get an interactive shell instead.
    With --boot the final command/shell runs inside a booted ephemeral container (systemd-nspawn --ephemeral --boot ...).
    With --dry-run the whole pipeline runs against in-memory fakes: the config is
    processed exactly as in a real build, but no storage or nspawn commands run.
    """

    logger = setup_logger(verbose=verbose)

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

    # === Parse & validate the config before touching the store ===
    try:
        spec = BuildSpec.from_yaml(prepare_yaml, variables=variables)
    except Exception as e:
        logger.error(f"ERROR: {e}")
        sys.exit(1)

    if profile not in spec.profiles:
        if profile == "base":
            logger.error("ERROR: 'base' is a reserved special profile and cannot be selected")
        else:
            logger.error(f"ERROR: Profile '{profile}' not found. Available: {list(spec.profiles.keys())}")
        sys.exit(1)

    selected_profile = spec.profiles[profile]

    post_cmd = list(command) if command else None

    if dry_run:
        # Full pipeline simulation: real YAML processing + fakes for storage/execution.
        # Runs in a private temp dir so the real container store is never touched.
        workdir = Path(tempfile.mkdtemp(prefix="fastcontainer-dryrun-"))
        try:
            dry_executor = RecordingExecutor()
            builder = Builder(
                containers_dir=workdir,
                spec=spec,
                profile=selected_profile,
                prune=prune,
                verbose=verbose,
                logger=logger,
                post_build_cmd=post_cmd,
                run_cmd=True,
                shell=shell,
                boot=boot,
                backstore=DirBackstore(),
                executor=dry_executor,
            )
            builder.build()
            logger.info(
                f"\nDRY RUN OK — {len(dry_executor.calls)} simulated operations. "
                f"The container store was not touched."
            )
        except Exception as e:
            # Same one-line error surface as real builds (e.g. missing base
            # without a create script) instead of a raw traceback.
            logger.error(f"ERROR: Dry run failed: {e}")
            sys.exit(1)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    # === Real build: exclusive lock for the entire build ===
    try:
        with acquire_build_lock(containers_dir):
            builder = Builder(
                containers_dir=containers_dir,
                spec=spec,
                profile=selected_profile,
                prune=prune,
                verbose=verbose,
                logger=logger,
                post_build_cmd=post_cmd,
                run_cmd=True,
                shell=shell,
                boot=boot,
            )
            builder.build()
    except BlockingIOError:
        logger.error(f"ERROR: Another fastcontainer build is already running in {containers_dir}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"ERROR: Build failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
