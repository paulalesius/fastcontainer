"""Regression tests for the post-refactor audit fixes.

- A6: stale temp subvolumes from interrupted builds are swept (all three
      prefixes: _<base>-create-*, _<effective>-temp-*, _<effective>-final-*)
- A7: env variable values are substituted verbatim - backslash sequences in a
      value are never interpreted as re backreferences
- A8: unknown top-level keys in an imported (or local) file produce a warning
      instead of vanishing silently
"""
import logging

from fastcontainer.models import BuildSpec
from conftest import WEB_YAML, do_build, write_yaml


# ─────────────────────────────────────────────────────────────────────────────
# A6 — stale temp cleanup covers create-, temp- and final- prefixes
# ─────────────────────────────────────────────────────────────────────────────

def test_stale_create_temp_and_final_dirs_are_swept(tmp_path, store_dir):
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name

    # Simulate leftovers from a previously interrupted build.
    for name in (
        f"_{spec.base.name}-create-deadbeef",
        f"_{eff}-temp-deadbeef",
        f"_{eff}-final-deadbeef",
        "_unrelated-leftover",
    ):
        (store_dir / name).mkdir()

    do_build(store_dir, yaml_path, "web")

    # __<base>-<40hex> dirs are the legitimate layer cache, not stale temps
    leftovers = {
        p.name for p in store_dir.iterdir()
        if p.name.startswith("_") and not p.name.startswith("__")
    }
    assert leftovers == {"_unrelated-leftover"}


# ─────────────────────────────────────────────────────────────────────────────
# A7 — backslash sequences in env values survive substitution verbatim
# ─────────────────────────────────────────────────────────────────────────────

def test_env_values_with_backslashes_are_substituted_verbatim(tmp_path):
    # Single-quoted YAML scalars keep the backslashes literal. If the
    # substitution ever regressed to a re.sub *string* replacement, a
    # backreference like \1 would re-insert the matched {{VAR}} token and
    # \2 would raise re.error.
    yaml_text = r"""
env:
  SED: 's/foo/\1/bar'
  TOOLPATH: 'C:\tools\bin'
  GRP: 'no group \2 here'
base:
  name: tb
  create: echo base
profiles:
  p:
    steps:
      - RUN: echo {{SED}} {{TOOLPATH}} {{GRP}}
"""
    yaml_path = write_yaml(tmp_path, yaml_text)
    spec = BuildSpec.from_yaml(yaml_path)

    assert spec.profiles["p"].steps[0].cmd == (
        r"echo s/foo/\1/bar C:\tools\bin no group \2 here"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A8 — unknown top-level keys in an imported file warn instead of vanishing
# ─────────────────────────────────────────────────────────────────────────────

def test_unknown_top_level_key_in_imported_file_warns(tmp_path, caplog):
    write_yaml(tmp_path, (
        "base:\n"
        "  name: tb\n"
        "  create: echo base\n"
        "profilez:\n"
        "  typo: oops\n"
    ), name="imp.yaml")
    write_yaml(tmp_path, (
        "import-base: imp.yaml\n"
        "profiles:\n"
        "  p:\n"
        "    steps:\n"
        "      - RUN: echo hi\n"
    ), name="loc.yaml")

    with caplog.at_level(logging.WARNING, logger="fastcontainer"):
        spec = BuildSpec.from_yaml(tmp_path / "loc.yaml")

    assert "profilez" in caplog.text
    assert "imp.yaml" in caplog.text
    # the typo'd key is ignored, the known ones still work
    assert list(spec.profiles) == ["p"]
    assert spec.base.name == "tb"


def test_unknown_top_level_key_in_local_file_warns(tmp_path, caplog):
    yaml_path = write_yaml(tmp_path, (
        "base:\n"
        "  name: tb\n"
        "  create: echo base\n"
        "profiles:\n"
        "  p:\n"
        "    steps:\n"
        "      - RUN: echo hi\n"
        "snippetz:\n"
        "  typo: oops\n"
    ))

    with caplog.at_level(logging.WARNING, logger="fastcontainer"):
        spec = BuildSpec.from_yaml(yaml_path)

    assert "snippetz" in caplog.text
    assert list(spec.profiles) == ["p"]
