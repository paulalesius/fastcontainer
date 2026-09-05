"""A4: base/profile names become directory names under the container store.

They are therefore validated at parse time as single path segments, which makes
it impossible for a name to ever point outside the store directory
('..', '/', backslashes, spaces, ...). Every name that flows into a path
(base paths, __layers, temp dirs, final image names) passes one of these
checks:

- base.name      -> BaseSpec.from_data (both the string and dict forms)
- profile names  -> BuildSpec.from_yaml (local AND imported, post-merge)
"""
import json

import pytest
from click.testing import CliRunner

from fastcontainer.cli import build
from fastcontainer.models import BuildSpec

from conftest import write_yaml


def make_base_yaml(tmp_path, name: str, file: str = "prepare.yaml"):
    """A minimal config whose base name is exactly *name* (json-quoting keeps
    every character literal inside the YAML)."""
    text = f"""base:
  name: {json.dumps(name)}
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo hi
"""
    return write_yaml(tmp_path, text, name=file)


@pytest.mark.parametrize("bad", [
    "../escaped",   # the original A4 traversal
    "..",
    "a/b",          # would create a subdirectory
    "a\\b",        # backslash separator (Windows-style)
    "has space",
    "-leading",     # starts with '-'
    ".hidden",      # starts with '.'
    "tab\\there",
])
def test_base_name_rejects_traversal_and_bad_chars(tmp_path, bad):
    with pytest.raises(ValueError, match="Invalid base name"):
        BuildSpec.from_yaml(make_base_yaml(tmp_path, bad))


def test_string_base_form_is_validated_too(tmp_path):
    p = write_yaml(tmp_path, """base: "../x"
profiles:
  p:
    steps:
      - RUN: echo hi
""")
    with pytest.raises(ValueError, match="Invalid base name"):
        BuildSpec.from_yaml(p)


def test_realistic_names_still_accepted(tmp_path):
    p = make_base_yaml(tmp_path, "ubuntu24.04-cu132-llama-cpp")
    spec = BuildSpec.from_yaml(p)
    assert spec.base.name == "ubuntu24.04-cu132-llama-cpp"


@pytest.mark.parametrize("bad", ["a/b", "../evil", "with space", "a\\b", " trailing"])
def test_profile_name_rejects_traversal_and_bad_chars(tmp_path, bad):
    text = f"""base:
  name: testbase
  create: "echo base"
profiles:
  {json.dumps(bad)}:
    steps:
      - RUN: echo hi
"""
    with pytest.raises(ValueError, match="Invalid profile name"):
        BuildSpec.from_yaml(write_yaml(tmp_path, text))


def test_imported_profile_name_is_validated_too(tmp_path):
    """The merge happens before validation, so a bad name in an imported
    library file is caught as well."""
    lib = write_yaml(
        tmp_path,
        """base:
  name: testbase
  create: "echo b"
profiles:
  evil/prof:
    steps:
      - RUN: echo x
""",
        name="lib.yaml",
    )
    main = write_yaml(tmp_path, f"import-base: {lib.name}\n", name="main.yaml")
    with pytest.raises(ValueError, match="Invalid profile name"):
        BuildSpec.from_yaml(main)


def test_cli_reports_invalid_base_name_cleanly(tmp_path, store_dir):
    p = make_base_yaml(tmp_path, "../escaped")
    r = CliRunner().invoke(build, [str(store_dir), str(p), "-p", "p"])
    assert r.exit_code == 1
    assert "Invalid base name" in r.output
    assert "Traceback" not in r.output


def test_cli_dry_run_reports_missing_base_cleanly(tmp_path, store_dir):
    """A5: the dry-run path used to let the raw FileNotFoundError escape as a
    traceback; it must now fail with the same one-line error as real builds."""
    p = write_yaml(tmp_path, """base:
  name: ghost
profiles:
  p:
    steps:
      - RUN: echo hi
""")
    r = CliRunner().invoke(build, [str(store_dir), str(p), "-p", "p", "--dry-run"])
    assert r.exit_code == 1
    assert "Base subvolume not found" in r.output
    assert "Traceback" not in r.output
