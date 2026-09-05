"""Unit tests for _prepare_nspawn_args (pure) and NspawnExecutor wiring.

NspawnExecutor normally requires root; these tests patch os.geteuid and the
subprocess seams so the argument construction can be verified unprivileged.
"""
import subprocess
from pathlib import Path

import pytest

from fastcontainer.executor import NspawnExecutor, _prepare_nspawn_args

ROOT = Path("/store/base-123")


def test_bare_template_gets_full_defaults():
    args = _prepare_nspawn_args(ROOT, ["systemd-nspawn"])
    assert args == [
        "systemd-nspawn",
        "-D", str(ROOT),
        "--user=root",
        "--register=no",
        "--hostname=fastcontainer",
        "--quiet",
    ]


def test_manual_directory_flags_are_stripped_and_injected():
    for template in (
        ["systemd-nspawn", "-D", "/manual", "--bind=/x"],
        ["systemd-nspawn", "--directory=/manual", "--bind=/x"],
        ["systemd-nspawn", "-D=/manual", "--bind=/x"],
    ):
        args = _prepare_nspawn_args(ROOT, template)
        assert args[0] == "systemd-nspawn"
        # automatic -D right after the program token, pointing at the real root
        assert args[1:3] == ["-D", str(ROOT)]
        assert "/manual" not in args
        assert "--bind=/x" in args


def test_user_flag_injection():
    assert "--user=root" in _prepare_nspawn_args(ROOT, ["systemd-nspawn"], user="root")
    assert "--user=root" in _prepare_nspawn_args(ROOT, ["systemd-nspawn"], user="")
    args = _prepare_nspawn_args(ROOT, ["systemd-nspawn"], user="appuser")
    assert "--user=appuser" in args
    assert "--user=root" not in args


def test_ephemeral_boot_placed_before_user_flags():
    args = _prepare_nspawn_args(ROOT, ["systemd-nspawn"], ephemeral=True, boot=True, user="bob")
    assert args.index("--ephemeral") < args.index("--user=bob")
    assert args.index("--boot") < args.index("--user=bob")
    assert args.index("--ephemeral") < args.index("--boot")


def test_safe_defaults_not_duplicated():
    template = ["systemd-nspawn", "--register=no", "--hostname=other", "--quiet"]
    args = _prepare_nspawn_args(ROOT, template)
    assert args.count("--register=no") == 1
    assert "--hostname=fastcontainer" not in args
    assert "--hostname=other" in args
    assert args.count("--quiet") == 1


def test_quiet_flag_respects_flag():
    assert "--quiet" in _prepare_nspawn_args(ROOT, ["systemd-nspawn"], quiet=True)
    assert "--quiet" not in _prepare_nspawn_args(ROOT, ["systemd-nspawn"], quiet=False)


def test_empty_template_falls_back_to_systemd_nspawn():
    args = _prepare_nspawn_args(ROOT, [])
    assert args[0] == "systemd-nspawn"
    assert args[1:3] == ["-D", str(ROOT)]


class TestNspawnExecutor:
    """Wiring of the real executor, verified without root or nspawn."""

    def test_requires_root(self, monkeypatch):
        monkeypatch.setattr("fastcontainer.executor.os.geteuid", lambda: 1000)
        with pytest.raises(PermissionError, match="requires root"):
            NspawnExecutor()

    @pytest.fixture
    def wired(self, monkeypatch):
        """NspawnExecutor with the process seams patched; records commands."""
        monkeypatch.setattr("fastcontainer.executor.os.geteuid", lambda: 0)
        recorded = {"run_and_capture": [], "subprocess": []}

        def fake_run_and_capture(cmd, verbose=False, cwd=None):
            recorded["run_and_capture"].append((cmd, cwd))
            return "ok\n"

        def fake_subprocess_run(cmd, check=True):
            recorded["subprocess"].append((cmd, check))
            return None

        monkeypatch.setattr("fastcontainer.executor.run_and_capture", fake_run_and_capture)
        monkeypatch.setattr("fastcontainer.executor.subprocess.run", fake_subprocess_run)
        return NspawnExecutor(), recorded

    def test_create_base_runs_script_on_host(self, wired):
        ex, rec = wired
        ex.create_base(ROOT, "echo made\n", verbose=False)
        (cmd, cwd), = rec["run_and_capture"]
        assert cmd == ["/bin/bash", "-c", "echo made\n"]
        assert cwd == ROOT

    def test_execute_wraps_in_strict_bash(self, wired):
        ex, rec = wired
        ex.execute(ROOT, "echo hi", ["systemd-nspawn"], user="bob")
        (cmd, _), = rec["run_and_capture"]
        assert cmd[0] == "systemd-nspawn"
        assert cmd[1:3] == ["-D", str(ROOT)]
        assert "--user=bob" in cmd
        assert cmd[-4:] == ["/bin/bash", "-l", "-c", "set -eo pipefail\necho hi"]

    def test_exec_in_string_and_list_commands(self, wired):
        ex, rec = wired
        ex.exec_in(ROOT, "echo one", ["systemd-nspawn"], user="root", ephemeral=True, boot=True)
        ex.exec_in(ROOT, ["echo", "two"], ["systemd-nspawn"], user="root", ephemeral=True, boot=True)
        ex.exec_in(ROOT, None, ["systemd-nspawn"], ephemeral=True, boot=True)
        ex.exec_in(ROOT, "", ["systemd-nspawn"], ephemeral=True, boot=True)

        (cmd1, _), (cmd2, _) = rec["subprocess"]
        assert "--ephemeral" in cmd1 and "--boot" in cmd1
        assert cmd1[-4:] == ["/bin/bash", "-l", "-c", "set -eo pipefail\necho one"]
        # quiet list commands run args + cmd verbatim: the raw command sits at the tail
        assert cmd2[-2:] == ["echo", "two"]
        assert "--quiet" in cmd2
        assert "--user=root" in cmd2

    def test_check_exit_semantics(self, wired):
        ex, rec = wired

        def ok(cmd, verbose=False, cwd=None):
            rec["run_and_capture"].append((cmd, cwd))
            return "fine\n"

        import fastcontainer.executor as fx
        fx.run_and_capture = ok
        assert ex.check(ROOT, "test -f /x", ["systemd-nspawn"]) is True
        # check does not use --quiet (failure output must stay visible)
        (cmd, _), = rec["run_and_capture"]
        assert "--quiet" not in cmd

        def fail(cmd, verbose=False, cwd=None):
            raise subprocess.CalledProcessError(1, cmd, "boom")

        fx.run_and_capture = fail
        assert ex.check(ROOT, "test -f /x", ["systemd-nspawn"]) is False

        def crash(cmd, verbose=False, cwd=None):
            raise RuntimeError("nspawn missing")

        fx.run_and_capture = crash
        assert ex.check(ROOT, "test -f /x", ["systemd-nspawn"]) is False

        # empty check passes without executing anything
        rec["run_and_capture"].clear()
        fx.run_and_capture = ok
        assert ex.check(ROOT, "", ["systemd-nspawn"]) is True
        assert rec["run_and_capture"] == []
