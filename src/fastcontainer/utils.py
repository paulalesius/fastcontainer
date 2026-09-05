import subprocess
import sys
from typing import List
from pathlib import Path

import logging
logger = logging.getLogger("fastcontainer")


class CommandFailedError(subprocess.CalledProcessError):
    """CalledProcessError whose str() carries the captured stderr.

    Plain CalledProcessError.str() (Python < 3.13) omits captured output, so
    a failing btrfs command would surface as a bare 'returned non-zero exit
    status N' with no diagnostics at all.
    """

    def __str__(self):
        base = super().__str__()
        if self.stderr:
            return f"{base} | stderr: {self.stderr.rstrip()}"
        return base


def run(
    cmd: List[str],
) -> None:
    """Execute internal command (btrfs, etc.). Silent on success; on failure
    the command's stderr is attached to the raised error so btrfs errors are
    visible to the user instead of being swallowed."""
    logger.debug("-> " + " ".join(map(str, cmd)))
    proc = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise CommandFailedError(proc.returncode, cmd, stderr=stderr or None)


def run_and_capture(
    cmd: List[str],
    verbose: bool = False,
    cwd: Path | str | None = None,
) -> str:
    """Run a command, capture output always.

    - verbose=True: live output (for -v mode)
    - On failure: ALWAYS print a very clear, boxed failure banner
      so the user immediately sees what went wrong.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    output_lines: list[str] = []
    for line in process.stdout:  # type: ignore
        output_lines.append(line)
        if verbose:
            sys.stdout.write(line)
            sys.stdout.flush()

    returncode = process.wait()
    output = "".join(output_lines)

    if returncode != 0:
        if not verbose:
            # === Much clearer failure banner (this is what you asked for) ===
            sys.stdout.write("\n" + "═" * 80 + "\n")
            sys.stdout.write(" " * 28 + "❌  BUILD STEP FAILED" + "\n")
            sys.stdout.write("═" * 80 + "\n\n")

            # Show the exact command that failed (super helpful now that -D is hidden)
            sys.stdout.write(f"Failed command:\n  {' '.join(map(str, cmd))}\n\n")

            sys.stdout.write("Step output:\n")
            sys.stdout.write(output.rstrip() + "\n\n")

            sys.stdout.write("═" * 80 + "\n")
            sys.stdout.write(" " * 25 + "END OF FAILED STEP OUTPUT" + "\n")
            sys.stdout.write("═" * 80 + "\n\n")
            sys.stdout.flush()

        raise subprocess.CalledProcessError(returncode, cmd, output)

    return output
