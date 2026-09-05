"""CLI tests: build-lock behaviour and click-level validation (CliRunner).

The root check was moved out of the CLI into the real backend constructors
(BtrfsBackstore / NspawnExecutor), so these tests run unprivileged.
"""
import pytest
from click.testing import CliRunner

from fastcontainer.cli import acquire_build_lock, build

from conftest import WEB_YAML, write_yaml


class TestBuildLock:
    def test_second_lock_is_blocked(self, tmp_path):
        d = tmp_path / "store"
        d.mkdir()
        with acquire_build_lock(d):
            with pytest.raises(BlockingIOError):
                with acquire_build_lock(d):
                    pass

    def test_lock_released_after_block(self, tmp_path):
        d = tmp_path / "store"
        d.mkdir()
        with acquire_build_lock(d):
            pass
        # released + cleaned up, and re-acquirable
        assert not (d / ".fastcontainer.lock").exists()
        with acquire_build_lock(d):
            pass
        assert not (d / ".fastcontainer.lock").exists()

    def test_lock_file_created_inside_store(self, tmp_path):
        d = tmp_path / "store"
        d.mkdir()
        with acquire_build_lock(d):
            assert (d / ".fastcontainer.lock").exists()


BASE_YAML = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo hi
"""


class TestCliValidation:
    def setup_method(self):
        self.runner = CliRunner()

    def invoke(self, *args, tmp_path):
        # click needs plain strings, not PosixPath objects
        return self.runner.invoke(build, [str(a) for a in args], obj=None)

    def test_build_help(self, tmp_path):
        r = self.runner.invoke(build, ["--help"])
        assert r.exit_code == 0
        assert "--dry-run" in r.output
        assert "-D" in r.output

    def test_invalid_dash_d_without_equals(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "p", "-D", "NOEQUALS", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "Invalid -D flag" in r.output

    def test_invalid_dash_d_key(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "p", "-D", "BAD KEY=1", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "Invalid variable name in -D" in r.output

    def test_profile_not_found_lists_available(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "nope", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "Profile 'nope' not found" in r.output
        assert "'p'" in r.output

    def test_base_is_reserved_profile(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, BASE_YAML)
        r = self.invoke(d, y, "-p", "base", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "reserved special profile" in r.output

    def test_invalid_yaml_errors_cleanly(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, "base: \"\"\n")
        r = self.invoke(d, y, "-p", "p", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "ERROR:" in r.output
        assert "base" in r.output

    def test_dry_run_builds_full_pipeline_without_touching_store(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, WEB_YAML)
        r = self.invoke(d, y, "-p", "web", "--dry-run", tmp_path=tmp_path)
        assert r.exit_code == 0, r.output
        assert "DRY RUN OK" in r.output
        # the real container store must be completely untouched
        assert list(d.iterdir()) == []

    def test_dry_run_catches_config_errors_before_simulating(self, tmp_path):
        d = tmp_path / "store"; d.mkdir()
        y = write_yaml(tmp_path, "import-base: ghost.yaml\n")
        r = self.invoke(d, y, "-p", "p", "--dry-run", tmp_path=tmp_path)
        assert r.exit_code == 1
        assert "ERROR:" in r.output
        assert list(d.iterdir()) == []
