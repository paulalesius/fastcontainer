import subprocess
from typing import List


def run(cmd: List[str], quiet: bool = False) -> None:
    """Print and execute a command safely."""
    if not quiet:
        print("→", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True)
