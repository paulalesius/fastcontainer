import subprocess
from typing import List


def run(cmd: List[str]) -> None:
    """Print and execute a command safely (no shell, no injection risk)."""
    print("→", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True)
