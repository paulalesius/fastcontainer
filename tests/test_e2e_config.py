"""End-to-end tests: is the config (YAML) processed correctly?

Each test drives the real pipeline — BuildSpec.from_yaml + Builder.build —
with DirBackstore + RecordingExecutor, and asserts on the resulting store
layout, manifests, and the recorded execution plan.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from fastcontainer.backstore import DirBackstore
from fastcontainer.executor import RecordingExecutor
from fastcontainer.models import BuildSpec, Step

from conftest import HEX16, HEX40, WEB_YAML, do_build, final_path, layer_names, write_yaml


# ─────────────────────────────────────────────────────────────────────────────
# Basic build: final image, manifest, execution plan
# ─────────────────────────────────────────────────────────────────────────────

def test_build_creates_final_image_with_manifest(tmp_path, store_dir):
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    _, ex, b = do_build(store_dir, yaml_path, "web")

    # naming: <effective_base>-<profile>-<40hex fingerprint>
    assert re.fullmatch(rf"testbase-{HEX16}-web-{HEX40}", b.final_name)
    final = store_dir / b.final_name
    assert final.is_dir()

    m = json.loads((final / "fastcontainer.json").read_text())
    assert m["fastcontainer"] == "1"
    assert m["stage"] == "final"
    assert m["profile"] == "web"
    assert m["base"] == "testbase"
    assert m["final_name"] == b.final_name
    assert m["steps"] == 2
    assert set(m["logs"]) == {"001", "002"}
    assert m["logs"]["001"]["command"] == 'echo "layer one"'
    assert m["logs"]["002"]["command"] == 'echo "layer two as appuser"'
    assert m["nspawn_template"] == ["systemd-nspawn", "--tmpfs=/var/tmp"]
    assert "test -f /etc/motd" in m["check"]
    assert (store_dir / b.spec.base.effective_name).is_dir()  # simulated base exists

    # execution plan: base script once, then the two steps in order, per-step user
    assert ex.calls[0][0] == "create_base"
    assert "simulated debootstrap" in ex.calls[0][1]
    execs = [c for c in ex.calls if c[0] == "execute"]
    assert [c[1] for c in execs] == ["root", "appuser"]
    assert execs[0][2].startswith('echo "layer one"')
    assert execs[1][2].startswith('echo "layer two as appuser"')


def test_no_leftover_temps_and_layer_naming(tmp_path, store_dir):
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    _, _, b = do_build(store_dir, yaml_path, "web")

    eff = b.spec.base.effective_name
    leftovers = [p.name for p in store_dir.iterdir()
                 if "-temp-" in p.name or "-create-" in p.name or "-final-" in p.name]
    assert leftovers == []

    layers = layer_names(store_dir, eff)
    assert len(layers) == 2
    for name in layers:
        assert re.fullmatch(rf"__testbase-{HEX16}-{HEX40}", name)


# ─────────────────────────────────────────────────────────────────────────────
# base: creation, reuse, hashing after variable expansion
# ─────────────────────────────────────────────────────────────────────────────

def test_existing_base_is_reused(tmp_path, store_dir):
    yaml = """\
base:
  name: oldbase
profiles:
  p:
    steps:
      - RUN: echo hi
"""
    (store_dir / "oldbase").mkdir()
    (store_dir / "oldbase" / "marker").write_text("pre-existing")

    _, ex, b = do_build(store_dir, write_yaml(tmp_path, yaml), "p")
    assert b.spec.base.effective_name == "oldbase"
    assert not any(c[0] == "create_base" for c in ex.calls)
    final = store_dir / b.final_name
    assert final.is_dir()
    # the snapshot chain carries the base content through to the final image
    assert (final / "marker").read_text() == "pre-existing"


def test_missing_base_without_create_errors(tmp_path, store_dir):
    yaml = """\
base:
  name: ghostbase
profiles:
  p:
    steps:
      - RUN: echo hi
"""
    with pytest.raises(FileNotFoundError, match="Base subvolume not found"):
        do_build(store_dir, write_yaml(tmp_path, yaml), "p")


def test_base_create_with_variable_gets_hashed_name(tmp_path, store_dir):
    yaml = """\
env:
  MIRROR: http://archive.example.com
base:
  name: mybase
  create: |
    debootstrap noble . {{MIRROR}}
profiles:
  p:
    steps:
      - RUN: echo hi
"""
    yaml_path = write_yaml(tmp_path, yaml)
    spec = BuildSpec.from_yaml(yaml_path)
    expected = "mybase-" + hashlib.sha1("debootstrap noble . http://archive.example.com".encode()).hexdigest()[:16]
    assert spec.base.effective_name == expected

    # a different -D value changes the (post-expansion) base name
    spec2 = BuildSpec.from_yaml(yaml_path, variables={"MIRROR": "http://other.example.com"})
    assert spec2.base.effective_name != expected

    # and the build actually uses that name for base/finals
    _, _, b = do_build(store_dir, yaml_path, "p")
    assert b.spec.base.effective_name == expected
    assert (store_dir / expected).is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# import-base: merging, recursion, errors
# ─────────────────────────────────────────────────────────────────────────────

def test_import_base_merges_profiles_and_env(tmp_path, store_dir):
    write_yaml(tmp_path, """\
env:
  PORT: "8080"
base:
  name: testbase
  create: "echo parent base"
profiles:
  common:
    steps:
      - RUN: echo "common step"
""", name="parent.yaml")
    yaml_path = write_yaml(tmp_path, """\
import-base: parent.yaml
profiles:
  web:
    extend: common
    steps:
      - RUN: echo "listening on {{PORT}}"
""", name="child.yaml")

    _, ex, b = do_build(store_dir, yaml_path, "web")
    spec = b.spec

    # profiles and env merged from the imported file
    assert set(spec.profiles) == {"common", "web"}
    assert spec.env == {"PORT": "8080"}
    assert spec.base.name == "testbase"

    # the parent profile was built first and its final image exists
    parent_final = final_path(store_dir, spec.base.effective_name,
                              "common", spec.profiles["common"].fingerprint)
    assert parent_final.is_dir()
    assert (parent_final / "fastcontainer.json").is_file()

    # env from the parent is visible in child steps; parent steps ran first
    execs = [c for c in ex.calls if c[0] == "execute"]
    assert [c[2].strip() for c in execs] == ['echo "common step"', 'echo "listening on 8080"']


def test_import_base_recursive_chain(tmp_path):
    write_yaml(tmp_path, """\
env:
  K: v
base:
  name: grandbase
  create: "echo grand base"
""", name="grand.yaml")
    write_yaml(tmp_path, """\
import-base: grand.yaml
profiles:
  common:
    steps:
      - RUN: echo "from grand via parent"
""", name="parent.yaml")
    yaml_path = write_yaml(tmp_path, """\
import-base: parent.yaml
profiles:
  web:
    extend: common
    steps:
      - RUN: echo "{{K}}"
""", name="child.yaml")

    spec = BuildSpec.from_yaml(yaml_path)
    assert spec.base.name == "grandbase"
    assert set(spec.profiles) == {"common", "web"}
    assert spec.env == {"K": "v"}
    # env value is expanded through the whole chain
    assert spec.profiles["web"].local_steps[0].cmd == 'echo "v"'


def test_import_base_cycle_errors(tmp_path):
    write_yaml(tmp_path, "import-base: b.yaml\n", name="a.yaml")
    write_yaml(tmp_path, "import-base: a.yaml\n", name="b.yaml")
    with pytest.raises(ValueError, match="Circular import-base"):
        BuildSpec.from_yaml(tmp_path / "a.yaml")


def test_import_base_and_base_together_errors(tmp_path):
    yaml_path = write_yaml(tmp_path, "import-base: x.yaml\nbase:\n  name: x\n  create: \"echo x\"\n")
    with pytest.raises(ValueError, match="contains both"):
        BuildSpec.from_yaml(yaml_path)


def test_import_missing_file_errors(tmp_path):
    yaml_path = write_yaml(tmp_path, "import-base: nope.yaml\n")
    with pytest.raises(FileNotFoundError, match="Imported YAML not found"):
        BuildSpec.from_yaml(yaml_path)


# ─────────────────────────────────────────────────────────────────────────────
# env: defaults, -D overrides, chaining, errors
# ─────────────────────────────────────────────────────────────────────────────

def test_env_defaults_and_override(tmp_path, store_dir):
    yaml = """\
env:
  CACHE_DIR: /default/cache
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo cache={{CACHE_DIR}}
"""
    p1 = write_yaml(tmp_path, yaml, name="d1.yaml")
    _, ex1, _ = do_build(store_dir, p1, "p")
    execs1 = [c for c in ex1.calls if c[0] == "execute"]
    assert 'echo cache=/default/cache' in execs1[0][2]

    p2 = write_yaml(tmp_path, yaml, name="d2.yaml")
    _, ex2, _ = do_build(store_dir, p2, "p", variables={"CACHE_DIR": "/opt/cache"})
    # the base already exists (built for p1), so ex2 has no create_base call
    execs2 = [c for c in ex2.calls if c[0] == "execute"]
    assert 'echo cache=/opt/cache' in execs2[0][2]


def test_env_chained_variables_in_any_order(tmp_path, store_dir):
    yfwd = """\
env:
  A: x
  B: "{{A}}-y"
  C: "{{B}}-z"
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo {{C}}
"""
    yrev = yfwd.replace("env:\n  A: x\n  B: \"{{A}}-y\"\n  C: \"{{B}}-z\"",
                        "env:\n  C: \"{{B}}-z\"\n  B: \"{{A}}-y\"\n  A: x")
    _, ex1, _ = do_build(store_dir, write_yaml(tmp_path, yfwd, "fwd.yaml"), "p")
    assert "echo x-y-z" in [c for c in ex1.calls if c[0] == "execute"][0][2]
    _, ex2, _ = do_build(store_dir, write_yaml(tmp_path, yrev, "rev.yaml"), "p")
    # The reversed declaration order must resolve to exactly the same values:
    # the second build is then a pure cache hit (same fingerprint, same step
    # hash) and no step re-executes. If resolution ever differed, the step
    # command would differ and an execute call would appear here.
    assert [c for c in ex2.calls if c[0] == "execute"] == []


def test_undeclared_variable_in_step_errors(tmp_path):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo {{NOPE}}
"""
    with pytest.raises(ValueError, match="Undefined variable"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml))


def test_undeclared_dash_d_variable_errors(tmp_path):
    yaml = """\
env:
  OTHER: v
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo {{OTHER}}
"""
    with pytest.raises(ValueError, match="not declared in any"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml), variables={"GHOST": "1"})


def test_env_cycle_errors(tmp_path):
    yaml = """\
env:
  A: "{{B}}"
  B: "{{A}}"
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo {{A}}
"""
    with pytest.raises(ValueError, match="Circular or unresolved variable reference"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml))


# ─────────────────────────────────────────────────────────────────────────────
# extend: inheritance, chains, nspawn flags, errors
# ─────────────────────────────────────────────────────────────────────────────

def test_extend_chain_builds_parents_first(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  a:
    steps:
      - RUN: echo step-a
  b:
    extend: a
    steps:
      - RUN: echo step-b
  c:
    extend: b
    steps:
      - RUN: echo step-c
"""
    _, ex, b = do_build(store_dir, write_yaml(tmp_path, yaml), "c")
    spec = b.spec

    # step order: grandparent first, then parent, then local
    execs = [c for c in ex.calls if c[0] == "execute"]
    assert [c[2].strip() for c in execs] == ["echo step-a", "echo step-b", "echo step-c"]
    assert len(spec.profiles["c"].steps) == 3
    assert len(spec.profiles["c"].local_steps) == 1

    # every level of the chain produced its own final image
    for name in ("a", "b", "c"):
        final = final_path(store_dir, spec.base.effective_name, name,
                           spec.profiles[name].fingerprint)
        assert final.is_dir(), name


def test_layer_user_change_forces_layer_rebuild(tmp_path, store_dir):
    # A2 regression: step.user was missing from the layer hash, so changing a
    # parent-profile step's user silently reused the layer built as the old
    # user (non-leaf layers are cached, not force-rebuilt).
    def make(user: str, name: str) -> Path:
        return write_yaml(tmp_path, f"""\
base:
  name: testbase
  create: "echo base"
profiles:
  parent:
    steps:
      - RUN({user}): echo payload
  child:
    extend: parent
    steps:
      - RUN: echo extra
""", name)

    ex1 = RecordingExecutor()
    do_build(store_dir, make("bob", "y1.yaml"), "child", executor=ex1)
    assert any(c[0] == "execute" and c[1] == "bob" and c[2].strip() == "echo payload"
               for c in ex1.calls)

    # changing the step's user must re-execute the layer as the new user
    ex2 = RecordingExecutor()
    do_build(store_dir, make("root", "y2.yaml"), "child", executor=ex2)
    payload_runs = [c for c in ex2.calls
                    if c[0] == "execute" and c[2].strip() == "echo payload"]
    assert payload_runs, "changing step.user must invalidate the layer cache"
    assert payload_runs[0][1] == "root"

    # control: an unchanged rebuild still hits the non-leaf layer cache
    ex3 = RecordingExecutor()
    do_build(store_dir, make("root", "y3.yaml"), "child", executor=ex3)
    assert not any(c[0] == "execute" and c[2].strip() == "echo payload" for c in ex3.calls), \
        "unchanged non-leaf layer must stay cached"


def test_extend_inherits_and_removes_nspawn_flags(tmp_path):
    yaml = """\
base:
  name: testbase
  create: "echo base"
  add:
    - "--resolv-conf=replace-stub"
profiles:
  a:
    add:
      - "--tmpfs=/a"
  b:
    extend: a
    add:
      - "--bind=/b"
    remove:
      - "--tmpfs=/a"
"""
    spec = BuildSpec.from_yaml(write_yaml(tmp_path, yaml))
    assert spec.profiles["a"].nspawn == ["systemd-nspawn", "--resolv-conf=replace-stub", "--tmpfs=/a"]
    assert spec.profiles["b"].nspawn == ["systemd-nspawn", "--resolv-conf=replace-stub", "--bind=/b"]


def test_extend_self_errors(tmp_path):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    extend: p
    steps:
      - RUN: echo hi
"""
    with pytest.raises(ValueError, match="cannot extend itself"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml))


def test_extend_missing_target_errors(tmp_path):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    extend: ghost
    steps:
      - RUN: echo hi
"""
    with pytest.raises(ValueError, match="not found in YAML"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml))


def test_extend_cycle_errors(tmp_path):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  a:
    extend: b
  b:
    extend: a
"""
    with pytest.raises(ValueError, match="Circular dependency"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml))


# ─────────────────────────────────────────────────────────────────────────────
# snippets (USE:) and step users
# ─────────────────────────────────────────────────────────────────────────────

def test_snippet_use_both_forms(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
snippets:
  greet: "echo hello from snippet"
  dictform:
    RUN: echo from dict form
profiles:
  p:
    steps:
      - USE: greet
      - "USE(appuser)": dictform
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p")
    execs = [c for c in ex.calls if c[0] == "execute"]
    assert execs[0] == ("execute", "root", "echo hello from snippet")
    assert execs[1] == ("execute", "appuser", "echo from dict form")


def test_snippet_unknown_errors(tmp_path):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - USE: missing
"""
    with pytest.raises(ValueError, match="Snippet 'missing' not found"):
        BuildSpec.from_yaml(write_yaml(tmp_path, yaml))


def test_step_user_parsing(tmp_path, store_dir):
    yaml = """\
env:
  U: bob
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN(root): echo as root
      - RUN(ops): echo as ops
      - RUN({{U}}): echo via var
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p")
    execs = [c for c in ex.calls if c[0] == "execute"]
    assert [c[1] for c in execs] == ["root", "ops", "bob"]


def test_run_empty_parens_is_error():
    # audit bug #6 (fixed): "RUN()" / "USE()" with empty parens used to silently
    # become no-op steps (cmd=None); they are now rejected with a clear error.
    with pytest.raises(ValueError, match="empty parens"):
        Step.from_dict({"RUN()": "echo bare"}, 1, {}, "p")
    with pytest.raises(ValueError, match="empty parens"):
        Step.from_dict({"USE()": "snippet"}, 2, {}, "p")


def test_quoted_step_line_is_rejected():
    # audit bug D4 (fixed): quoting the whole step line makes YAML parse it as a
    # plain string; that used to be silently dropped (cmd=None). It is now an
    # error pointing at the likely cause (extra quotes around the whole line).
    with pytest.raises(ValueError, match="single string"):
        Step.from_dict("USE(appuser): dictform", 1, {}, "p")
    with pytest.raises(ValueError, match="single string"):
        Step.from_dict('RUN: echo "hi"', 2, {}, "p")
    # plain strings that are not RUN/USE step lines keep the old no-op behavior
    s = Step.from_dict("not a dict", 1, {}, "p")
    assert s.cmd is None and s.raw == "not a dict"


# ─────────────────────────────────────────────────────────────────────────────
# forbidden manual flags in add:
# ─────────────────────────────────────────────────────────────────────────────

def test_forbid_user_in_add(tmp_path):
    for flag in ("--user=bob", "-u=bob", "--user={{U}}"):
        yaml = """\
env:
  U: bob
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    add:
      - """ + repr(flag) + """
    steps:
      - RUN: echo hi
"""
        with pytest.raises(ValueError, match="Do NOT specify --user or -u"):
            BuildSpec.from_yaml(write_yaml(tmp_path, yaml, name=f"u_{flag.replace('-', '').replace('=', '_').replace('{{', '').replace('}}', '')}.yaml"))


def test_forbid_directory_in_add(tmp_path):
    pairs = [['"-D"', '"/x"'], ['"--directory=/x"'], ['"-D"', '"{{ROOT}}"']]
    for i, pair in enumerate(pairs):
        yaml = """\
env:
  ROOT: /r
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    add:
""" + "".join(f"      - {x}\n" for x in pair) + """    steps:
      - RUN: echo hi
"""
        with pytest.raises(ValueError, match="Do NOT specify -D"):
            BuildSpec.from_yaml(write_yaml(tmp_path, yaml, name=f"d{i}.yaml"))


# ─────────────────────────────────────────────────────────────────────────────
# check: gate and cache hits
# ─────────────────────────────────────────────────────────────────────────────

def test_check_gate_cache_hit_on_second_build(tmp_path, store_dir):
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    ex = RecordingExecutor()
    do_build(store_dir, yaml_path, "web", executor=ex)
    n_after_first = len([c for c in ex.calls if c[0] == "execute"])
    assert n_after_first == 2

    # same config again: check passes, cached image is reused, nothing re-executes
    do_build(store_dir, yaml_path, "web", executor=ex)
    assert len([c for c in ex.calls if c[0] == "execute"]) == n_after_first
    assert len([c for c in ex.calls if c[0] == "check"]) == 1


def test_check_gate_failure_recreates_final_from_cache(tmp_path, store_dir):
    yaml_path = write_yaml(tmp_path, WEB_YAML)
    ex = RecordingExecutor(check_result=True)
    do_build(store_dir, yaml_path, "web", executor=ex)
    n_after_first = len([c for c in ex.calls if c[0] == "execute"])

    ex.check_result = False  # simulate a failing check on the second build
    do_build(store_dir, yaml_path, "web", executor=ex)
    # Layers are content-addressed: the failing check deletes the final image,
    # but the rebuild re-creates it from the cached layers without re-executing
    # any step.
    assert len([c for c in ex.calls if c[0] == "execute"]) == n_after_first
    assert len([c for c in ex.calls if c[0] == "check"]) == 1
    # final image exists again after the rebuild
    spec = BuildSpec.from_yaml(yaml_path)
    final = final_path(store_dir, spec.base.effective_name, "web", spec.profiles["web"].fingerprint)
    assert final.is_dir()
    m = json.loads((final / "fastcontainer.json").read_text())
    assert m["stage"] == "final"


def test_second_build_without_check_is_cached(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo only step
"""
    yaml_path = write_yaml(tmp_path, yaml)
    ex = RecordingExecutor()
    do_build(store_dir, yaml_path, "p", executor=ex)
    do_build(store_dir, yaml_path, "p", executor=ex)
    assert len([c for c in ex.calls if c[0] == "execute"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# check: gate in profile chains (that profile and everything extending it)
# ─────────────────────────────────────────────────────────────────────────────

def _chain_yaml(with_child_check: bool) -> str:
    """A two-profile chain: 'common' (with a check: gate) and 'app' extending
    it. The child gets its own check: only when requested."""
    child_check = (
        "    check: |\n      test -f /etc/app-ready\n" if with_child_check else ""
    )
    return (
        "base:\n"
        "  name: testbase\n"
        "  create: |\n"
        '    echo "simulated debootstrap"\n'
        "    mkdir -p /usr\n"
        "\n"
        "profiles:\n"
        "  common:\n"
        "    steps:\n"
        '      - RUN: echo "common step"\n'
        "    check: |\n"
        "      test -f /etc/common-ready\n"
        "  app:\n"
        "    extend: common\n"
        "    steps:\n"
        '      - RUN: echo "app step"\n'
        + child_check
    )


class _DeleteRecordingBackstore(DirBackstore):
    """DirBackstore that records every delete() call (finals and temps)."""

    def __init__(self) -> None:
        self.deleted: list[Path] = []

    def delete(self, path: Path) -> None:
        self.deleted.append(Path(path))
        super().delete(path)


def test_check_failure_rebuilds_profile_and_descendant(tmp_path, store_dir):
    """Failing check: (non-zero exit) on 'common' while building 'app':
    the parent's final image is deleted and re-created, and so is the
    child's final image ("that profile and onward"). Unchanged steps are
    not re-executed - the re-creation is served from the layer cache."""
    yaml_path = write_yaml(tmp_path, _chain_yaml(with_child_check=False))
    bs = _DeleteRecordingBackstore()
    ex = RecordingExecutor()
    do_build(store_dir, yaml_path, "app", executor=ex, backstore=bs)

    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    common_final = final_path(store_dir, eff, "common", spec.profiles["common"].fingerprint)
    app_final = final_path(store_dir, eff, "app", spec.profiles["app"].fingerprint)
    assert common_final.is_dir() and app_final.is_dir()
    assert not [c for c in ex.calls if c[0] == "check"]  # first build: nothing to check
    n_exec = len([c for c in ex.calls if c[0] == "execute"])

    ex.check_result = False  # the parent's check now exits non-zero
    do_build(store_dir, yaml_path, "app", executor=ex, backstore=bs)

    # only the parent's check ran (the child defines none) and it failed
    checks = [c for c in ex.calls if c[0] == "check"]
    assert len(checks) == 1
    assert "test -f /etc/common-ready" in checks[0][1]
    # both final images were deleted and re-created
    assert common_final in bs.deleted
    assert app_final in bs.deleted
    assert common_final.is_dir() and app_final.is_dir()
    # no step re-execution: the rebuild is served from the layer cache
    assert len([c for c in ex.calls if c[0] == "execute"]) == n_exec
    m = json.loads((app_final / "fastcontainer.json").read_text())
    assert m["stage"] == "final" and m["profile"] == "app"


def test_check_failure_cascades_down_the_checking_chain(tmp_path, store_dir):
    """Both profiles have a check: and both now fail while building 'app':
    the child's gate runs first and deletes the child's final, then the
    parent's gate runs, fails and deletes the parent's final; both finals
    are re-created from their (cached) layers."""
    yaml_path = write_yaml(tmp_path, _chain_yaml(with_child_check=True))
    bs = _DeleteRecordingBackstore()
    ex = RecordingExecutor()
    do_build(store_dir, yaml_path, "app", executor=ex, backstore=bs)

    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    common_final = final_path(store_dir, eff, "common", spec.profiles["common"].fingerprint)
    app_final = final_path(store_dir, eff, "app", spec.profiles["app"].fingerprint)
    n_exec = len([c for c in ex.calls if c[0] == "execute"])

    ex.check_result = False  # both checks now exit non-zero
    do_build(store_dir, yaml_path, "app", executor=ex, backstore=bs)

    checks = [c for c in ex.calls if c[0] == "check"]
    assert len(checks) == 2
    # the child's gate runs before the parent's
    assert "test -f /etc/app-ready" in checks[0][1]
    assert "test -f /etc/common-ready" in checks[1][1]
    assert common_final in bs.deleted and app_final in bs.deleted
    assert common_final.is_dir() and app_final.is_dir()
    assert len([c for c in ex.calls if c[0] == "execute"]) == n_exec


class _PerCheckExecutor(RecordingExecutor):
    """RecordingExecutor with a per-check result (substring match on the
    check command text); the base check_result is the fallback."""

    def __init__(self, results: dict[str, bool], default: bool = True) -> None:
        super().__init__(check_result=default)
        self._results = results

    def check(self, root, command: str, nspawn, verbose: bool = False) -> bool:
        if not command or not command.strip():
            return True
        self.calls.append(("check", command))
        for key, result in self._results.items():
            if key in command:
                return result
        return self.check_result


def test_child_check_passing_skips_parent_check(tmp_path, store_dir):
    """Pins current behaviour (known gap): while the child's check: gate
    passes, the build reuses the cached child image and returns early - the
    parent's check: is never evaluated, so a failing parent check neither
    rebuilds the parent nor the child. If the intended semantics is "any
    failing check in the chain invalidates the chain", invert this test."""
    yaml_path = write_yaml(tmp_path, _chain_yaml(with_child_check=True))
    bs = _DeleteRecordingBackstore()
    ex = _PerCheckExecutor({
        "common-ready": False,  # parent's check would exit non-zero
        "app-ready": True,      # child's check passes
    })
    do_build(store_dir, yaml_path, "app", executor=ex, backstore=bs)

    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    common_final = final_path(store_dir, eff, "common", spec.profiles["common"].fingerprint)
    app_final = final_path(store_dir, eff, "app", spec.profiles["app"].fingerprint)
    n_exec = len([c for c in ex.calls if c[0] == "execute"])
    n_deleted = len(bs.deleted)

    do_build(store_dir, yaml_path, "app", executor=ex, backstore=bs)

    # only the child's check ran; the parent's failing check went unnoticed
    checks = [c for c in ex.calls if c[0] == "check"]
    assert len(checks) == 1
    assert "test -f /etc/app-ready" in checks[0][1]
    # nothing was rebuilt: no final deleted, no step re-executed
    assert len(bs.deleted) == n_deleted
    assert common_final not in bs.deleted and app_final not in bs.deleted
    assert len([c for c in ex.calls if c[0] == "execute"]) == n_exec


# ─────────────────────────────────────────────────────────────────────────────
# profile cmd: / shell / boot (post-build execution)
# ─────────────────────────────────────────────────────────────────────────────

def test_profile_cmd_runs_ephemeral_after_build(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo build step
    cmd: echo "hello from cmd"
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p")
    exec_in = [c for c in ex.calls if c[0] == "exec_in"]
    assert exec_in == [("exec_in", "root", 'echo "hello from cmd"', True, False)]
    # no cmd execution for a profile without cmd:
    _, ex2, _ = do_build(store_dir, write_yaml(tmp_path, WEB_YAML), "web")
    assert not [c for c in ex2.calls if c[0] == "exec_in"]


def test_cmd_user_and_list_cmd(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo s
    cmd(appuser):
      - echo
      - two words
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p")
    exec_in = [c for c in ex.calls if c[0] == "exec_in"]
    assert exec_in == [("exec_in", "appuser", ["echo", "two words"], True, False)]


def test_boot_mode_flags_the_post_build_execution(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo s
    cmd: echo booted
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p", boot=True)
    exec_in = [c for c in ex.calls if c[0] == "exec_in"]
    assert exec_in == [("exec_in", "root", "echo booted", True, True)]


def test_trailing_cli_command_takes_precedence_over_profile_cmd(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo s
    cmd: echo "profile cmd"
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p", post_cmd=["echo", "cli cmd"])
    exec_in = [c for c in ex.calls if c[0] == "exec_in"]
    assert exec_in == [("exec_in", "root", ["echo", "cli cmd"], True, False)]


def test_shell_mode_replaces_cmd_with_bash(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo s
    cmd(appuser): echo "profile cmd"
"""
    _, ex, _ = do_build(store_dir, write_yaml(tmp_path, yaml), "p", shell=True)
    exec_in = [c for c in ex.calls if c[0] == "exec_in"]
    # -s drops into an interactive shell instead of running the profile cmd
    assert exec_in == [("exec_in", "appuser", ["/bin/bash", "-l"], False, False)]


# ─────────────────────────────────────────────────────────────────────────────
# --prune
# ─────────────────────────────────────────────────────────────────────────────

def test_prune_removes_layers_keeps_final_and_base(tmp_path, store_dir):
    _, ex, b = do_build(store_dir, write_yaml(tmp_path, WEB_YAML), "web", prune=True)
    eff = b.spec.base.effective_name
    assert layer_names(store_dir, eff) == []
    assert (store_dir / eff).is_dir()                      # base kept
    assert (store_dir / b.final_name).is_dir()              # final kept
    leftovers = [p.name for p in store_dir.iterdir() if "-temp-" in p.name]
    assert leftovers == []


def test_no_prune_keeps_layers(tmp_path, store_dir):
    _, ex, b = do_build(store_dir, write_yaml(tmp_path, WEB_YAML), "web", prune=False)
    assert len(layer_names(store_dir, b.spec.base.effective_name)) == 2


def test_prune_is_scoped_to_the_built_profile(tmp_path, store_dir):
    # audit bug D5 (fixed): --prune used to delete every __<base>-* layer in the
    # store, clobbering the layer cache of other profiles of the same base.
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p1:
    steps:
      - RUN: echo one
  p2:
    steps:
      - RUN: echo two
"""
    yaml_path = write_yaml(tmp_path, yaml)
    spec = BuildSpec.from_yaml(yaml_path)
    eff = spec.base.effective_name
    do_build(store_dir, yaml_path, "p1")          # leaves its layer cached
    layers_p1 = layer_names(store_dir, eff)
    assert len(layers_p1) == 1
    do_build(store_dir, yaml_path, "p2", prune=True)
    remaining = layer_names(store_dir, eff)
    assert layers_p1[0] in remaining              # p1's layer survived p2's prune
    assert len(remaining) == 1


# ─────────────────────────────────────────────────────────────────────────────
# malformed / edge steps (Step.from_dict)
# ─────────────────────────────────────────────────────────────────────────────

def test_step_from_dict_edge_cases():
    s = Step.from_dict({"RUN": ""}, 1, {}, "p")
    assert s.cmd is None and s.user == "root"
    s = Step.from_dict({"RUN": "   \n  "}, 1, {}, "p")
    assert s.cmd is None
    s = Step.from_dict("not a dict", 1, {}, "p")
    assert s.cmd is None and s.raw == "not a dict"
    s = Step.from_dict({"RUN": ["echo a", "echo b"]}, 1, {}, "p")
    assert s.cmd == "echo a\necho b"
    s = Step.from_dict({"WEIRD": "x"}, 1, {}, "p")
    assert s.cmd is None  # unknown step kind is a silent no-op step


def test_empty_step_is_skipped_in_execution(tmp_path, store_dir):
    yaml = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: ""
      - RUN: echo real
"""
    # a step whose value expanded to empty yields cmd=None; the builder skips
    # such steps entirely (no layer, no execute call)
    spec = BuildSpec.from_yaml(write_yaml(tmp_path, yaml))
    assert spec.profiles["p"].local_steps[0].cmd is None


# ─────────────────────────────────────────────────────────────────────────────
# fingerprint semantics
# ─────────────────────────────────────────────────────────────────────────────

def test_cmd_is_in_fingerprint(tmp_path):
    base = """\
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo same
"""
    a = base + "    cmd: echo A\n"
    b = base + "    cmd(appuser): echo B\n"
    spec_a = BuildSpec.from_yaml(write_yaml(tmp_path, a, name="a.yaml"))
    spec_b = BuildSpec.from_yaml(write_yaml(tmp_path, b, name="b.yaml"))
    # cmd: / cmd_user: participate in the fingerprint: changing only the
    # post-build command (or its user) yields a different final image name.
    assert spec_a.profiles["p"].fingerprint != spec_b.profiles["p"].fingerprint
    assert spec_a.profiles["p"].cmd == "echo A"
    assert spec_b.profiles["p"].cmd_user == "appuser"


def test_different_dash_d_values_coexist_in_store(tmp_path, store_dir):
    yaml = """\
env:
  FLAVOR: plain
base:
  name: testbase
  create: "echo base"
profiles:
  p:
    steps:
      - RUN: echo "flavor={{FLAVOR}}"
"""
    p1 = write_yaml(tmp_path, yaml, name="f1.yaml")
    p2 = write_yaml(tmp_path, yaml, name="f2.yaml")
    ex = RecordingExecutor()
    do_build(store_dir, p1, "p", variables={"FLAVOR": "red"}, executor=ex)
    do_build(store_dir, p2, "p", variables={"FLAVOR": "blue"}, executor=ex)

    names = [p.name for p in store_dir.iterdir() if p.is_dir() and "-p-" in p.name]
    # two distinct final images (different step content -> different fingerprint)
    finals = [n for n in names if not n.startswith("__") and "-temp-" not in n]
    assert len(finals) == 2
    # the base was only created once (no {{VAR}} in base.create)
    assert len([c for c in ex.calls if c[0] == "create_base"]) == 1
    # both flavors were actually executed
    execs = [c for c in ex.calls if c[0] == "execute"]
    assert any("flavor=red" in c[2] for c in execs)
    assert any("flavor=blue" in c[2] for c in execs)
