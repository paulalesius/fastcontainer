"""utils.run: internal (btrfs) commands stay silent on success but must
surface their stderr on failure - a bare 'returned non-zero exit status N'
is undebuggable (A3)."""
import subprocess

import pytest

from fastcontainer.utils import CommandFailedError, run


def test_run_success_is_silent():
    run(["/bin/sh", "-c", "echo hidden; echo also-hidden 1>&2"])


def test_run_failure_carries_stderr():
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run(["/bin/sh", "-c", "echo boom 1>&2; exit 3"])
    err = exc_info.value
    assert isinstance(err, CommandFailedError)
    assert err.returncode == 3
    assert "boom" in (err.stderr or "")
    # the CLI reports failures as "Build failed: {e}" - the stderr must be
    # part of str(e); plain CalledProcessError.str() omits it before py3.13
    assert "boom" in str(err)


def test_run_failure_without_stderr_still_raises():
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run(["/bin/sh", "-c", "exit 7"])
    assert exc_info.value.returncode == 7
    assert "stderr" not in str(exc_info.value).lower() or not exc_info.value.stderr
