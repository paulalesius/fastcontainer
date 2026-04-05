import subprocess
import sys
from typing import List


def run(
    cmd: List[str],
    quiet: bool = False,
    capture_output: bool = False,
) -> None:
    """Print and execute a command safely.

    When quiet=True and capture_output=True, the tool's normal output
    is suppressed (only errors are shown).
    """
    if not quiet:
        print("→", " ".join(map(str, cmd)))

    if quiet and capture_output:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"❌ Command failed: {' '.join(map(str, cmd))}", file=sys.stderr)
            if result.stdout.strip():
                print(result.stdout, end="", file=sys.stderr)
            if result.stderr.strip():
                print(result.stderr, end="", file=sys.stderr)
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
    else:
        subprocess.run(cmd, check=True)
