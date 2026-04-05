# fastcontainer/fastcontainer/cli.py
#!/usr/bin/env python3
"""
fastcontainer CLI — built with Click
"""
import os
import sys
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

    try:
        spec = BuildSpec.from_yaml(prepare_yaml)
    except FileNotFoundError as e:
        click.secho(f"ERROR: {e}", fg="red")
        sys.exit(1)
    except ValueError as e:
        click.secho(f"ERROR: Invalid YAML – {e}", fg="red")
        sys.exit(1)
    except Exception as e:
        click.secho(f"ERROR: Failed to parse YAML: {e}", fg="red")
        sys.exit(1)

    # Safety checks (exact same as original)
    base_path = containers_dir / spec.base
    if not base_path.is_dir():
        click.secho(f"ERROR: Base subvolume not found: {base_path}", fg="red")
        sys.exit(1)

    if not base_path.resolve().is_relative_to(containers_dir):
        click.secho("ERROR: Base subvolume must stay inside the containers directory", fg="red")
        sys.exit(1)

    builder = Builder(containers_dir=containers_dir, spec=spec)

    try:
        builder.build()
    except Exception as e:
        click.secho(f"❌ Build failed: {e}", fg="red")
        sys.exit(1)


if __name__ == "__main__":
    main()
