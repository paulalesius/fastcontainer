from pathlib import Path
from typing import List

from .utils import run_and_capture


def execute(root: Path, command: str, nspawn_template: List[str], quiet: bool = False) -> str:
    """Execute a command inside the container using the selected profile's nspawn template."""
    # Replace placeholder {{ROOT}} with actual container root path
    args = [arg.replace("{{ROOT}}", str(root)) for arg in nspawn_template]

    # Add --quiet flag if requested and not already present
    if quiet and "--quiet" not in args:
        args.append("--quiet")

    args += ["/bin/bash", "-l", "-c", command]

    return run_and_capture(args, quiet=quiet)
