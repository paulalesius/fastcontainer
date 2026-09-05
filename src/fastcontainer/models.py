from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

import yaml

logger = logging.getLogger("fastcontainer")


def _expand_variables(text: str, variables: dict[str, str], context: str) -> str:
    """Expand ONLY {{VAR}} syntax using variables declared in env: (plus -D overrides)."""
    if not text or not isinstance(text, str):
        return text

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1).strip()
        if var_name not in variables:
            raise ValueError(
                f"{context}: Undefined variable '{{{{ {var_name} }}}}'.\n"
                f"It must be declared in the top-level 'env:' section of the YAML "
                f"(with an optional default value).\n"
                f"You can override the default with -D {var_name}=value on the command line."
            )
        return variables[var_name]

    return re.sub(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}', replacer, text)

def _resolve_yaml(yaml_path: Path, visited: set[Path] | None = None) -> dict:
    """Recursively resolve import-base chains and merge sections (local values override imported)."""
    if visited is None:
        visited = set()

    resolved_path = yaml_path.resolve()
    if resolved_path in visited:
        raise ValueError(f"Circular import-base detected involving {yaml_path}")
    visited.add(resolved_path)

    with open(yaml_path, "r", encoding="utf-8") as f:
        raw: dict = yaml.safe_load(f) or {}

    import_base = raw.get("import-base")
    if import_base is not None:
        if raw.get("base") is not None:
            raise ValueError(
                f"YAML {yaml_path.name} contains both 'base:' and 'import-base:'. "
                "Only one is allowed."
            )

        import_base = raw.pop("import-base")
        import_path = (yaml_path.parent / import_base).resolve()
        if not import_path.is_file():
            raise FileNotFoundError(f"Imported YAML not found: {import_path}")

        imported = _resolve_yaml(import_path, visited)

        # Merge: imported → raw (raw/local always wins for base, profiles, snippets, env)
        for key in ("base", "profiles", "snippets", "env"):
            if key in imported:
                imported_val = imported[key]
                local_val = raw.get(key)
                if isinstance(imported_val, dict) and isinstance(local_val, dict):
                    merged = dict(imported_val)
                    merged.update(local_val)
                    raw[key] = merged
                elif local_val is None:
                    raw[key] = imported_val

    # Warn about top-level keys the builder does not know about: without this
    # they vanish silently (e.g. a typo like 'profilez:' in an imported
    # library file would be dropped with no trace).
    for key in sorted(set(raw) - {"base", "profiles", "snippets", "env"}):
        logger.warning(
            f"{yaml_path.name}: unrecognized top-level key '{key}' was ignored "
            f"(expected one of: base, profiles, snippets, env)"
        )

    visited.remove(resolved_path)
    return raw

def _forbid_manual_directory(profile_name: str, flags: List[str]) -> None:
    """Completely forbid the user from specifying the root directory."""
    for item in flags:
        flag = str(item).strip()
        if flag in ("-D", "--directory") or flag.startswith(("--directory=", "-D=")):
            raise ValueError(
                f"Profile '{profile_name}': Do NOT specify -D, --directory or any root path.\n"
                f"fastcontainer automatically adds '-D <root>' for you.\n"
                f"Remove any such lines from your 'add:' section."
            )


def _forbid_manual_user(profile_name: str, flags: List[str]) -> None:
    """Completely forbid --user / -u in add:."""
    for item in flags:
        flag = str(item).strip()
        if flag in ("--user", "-u") or flag.startswith(("--user=", "-u=")):
            raise ValueError(
                f"Profile '{profile_name}': Do NOT specify --user or -u in 'add:'.\n"
                f"Use the new per-step syntax instead:\n"
                f"    - RUN(username): | ...\n"
                f"    - USE(username): snippet-name"
            )


def _parse_step_key(key: str) -> tuple[str, str | None]:
    """Parse 'RUN', 'RUN(root)', 'USE(noname)', 'RUN({{USER}})', etc."""
    import re
    match = re.match(r'^(RUN|USE)\s*(?:\(([^)]+)\))?$', key.strip())
    if not match:
        return key.strip(), None
    cmd_type = match.group(1)
    user = match.group(2).strip() if match.group(2) else None
    return cmd_type, user


_NAME_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_container_name(kind: str, name: str) -> None:
    """Validate a name that becomes a directory name inside the container store.

    Base and profile names are joined into paths under containers_dir, so they
    must be single path segments: no separators ('/', '\\'), no '..' and no
    leading '.' or '-'. Validating at parse time makes it impossible for a name
    to ever point outside the store directory.
    """
    if not _NAME_ALLOWED.match(name):
        raise ValueError(
            f"Invalid {kind} name: '{name}'.\n"
            f"  {kind.capitalize()} names become directory names in the container store,\n"
            f"  so they may only contain letters, digits, '.', '_' and '-',\n"
            f"  and must start with a letter or digit.\n"
            f"  (Path separators, '..' and other characters are not allowed.)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# All the other classes (NspawnProfile, BaseSpec, Step, Layer, Manifest) stay
# exactly the same as in your current file — only the BuildSpec.from_yaml changes.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NspawnProfile:
    name: str
    nspawn: List[str]
    cmd: List[str] | str | None = None
    cmd_user: str = "root"
    steps: List[Step] = field(default_factory=list)
    parent: str | None = None
    local_steps: List[Step] = field(default_factory=list)
    check: str | None = None

    @classmethod
    def from_dict(
        cls,
        name: str,
        data: dict,
        resolved_profiles: Dict[str, "NspawnProfile"],
        base_nspawn: List[str] | None = None,
        variables: dict[str, str] | None = None,
        snippets: Dict[str, str] | None = None,
    ) -> "NspawnProfile":
        if variables is None:
            variables = {}
        if snippets is None:
            snippets = {}

        # (rest of this method is 100% unchanged from your current file)
        extend_name = data.get("extend")
        add_raw = data.get("add", [])
        remove_raw = data.get("remove", data.get("del", []))
        steps_raw = data.get("steps", [])
        check_raw = data.get("check")

        if not isinstance(add_raw, list):
            raise ValueError(f"Profile '{name}' 'add:' must be a list")
        if not isinstance(remove_raw, list):
            raise ValueError(f"Profile '{name}' 'remove:' must be a list")
        if not isinstance(steps_raw, list):
            raise ValueError(f"Profile '{name}' 'steps:' must be a list of step dicts (RUN: ...)")

        if extend_name:
            if extend_name not in resolved_profiles:
                raise ValueError(f"Profile '{name}' extends unknown profile '{extend_name}'")
            effective = resolved_profiles[extend_name].nspawn[:]
        else:
            effective = ["systemd-nspawn"]
            if base_nspawn:
                for flag in base_nspawn:
                    if flag and flag not in effective:
                        effective.append(flag)

        for item in add_raw:
            flag = str(item).strip()
            if flag and flag not in effective:
                effective.append(flag)

        remove_set = {str(item).strip() for item in remove_raw if str(item).strip()}
        effective = [flag for flag in effective if flag not in remove_set]

        _forbid_manual_user(name, effective)

        effective = [
            _expand_variables(str(flag), variables, f"Profile '{name}' add:")
            for flag in effective
        ]

        _forbid_manual_directory(name, effective)

        parsed_local_steps: List[Step] = []
        for i, s in enumerate(steps_raw, 1):
            parsed_local_steps.append(
                Step.from_dict(s, i, variables=variables, profile_name=name, snippets=snippets)
            )

        if extend_name:
            parent = resolved_profiles[extend_name]
            effective_steps: List[Step] = list(parent.steps)
            for idx, s in enumerate(parsed_local_steps, len(parent.steps) + 1):
                effective_steps.append(replace(s, index=idx))
            local_steps = parsed_local_steps
            parent_name = extend_name
        else:
            effective_steps = parsed_local_steps
            local_steps = parsed_local_steps
            parent_name = None

        cmd: List[str] | str | None = None
        cmd_user: str = "root"
        cmd_key = None
        cmd_value = None
        for k, v in list(data.items()):
            k_str = str(k).strip()
            if k_str == "cmd" or (k_str.startswith("cmd(") and k_str.endswith(")")):
                cmd_key = k_str
                cmd_value = v
                data.pop(k, None)
                break

        if cmd_key is not None:
            if cmd_key != "cmd":
                match = re.search(r'cmd\(([^)]+)\)', cmd_key)
                if match:
                    user_raw = match.group(1).strip()
                    cmd_user = _expand_variables(
                        user_raw, variables, f"Profile '{name}' cmd: user"
                    ).strip() or "root"

            value = cmd_value
            if isinstance(value, str):
                cmd_str = value.strip()
                if cmd_str:
                    cmd_str = _expand_variables(cmd_str, variables, f"Profile '{name}' cmd:")
                    cmd = cmd_str
            elif isinstance(value, list):
                cmd_list = [
                    _expand_variables(str(x).strip(), variables, f"Profile '{name}' cmd:")
                    for x in value if str(x).strip()
                ]
                cmd = cmd_list if cmd_list else None
            else:
                raise ValueError(f"Profile '{name}' cmd: must be a string or list (or cmd(user): form)")

        check: str | None = None
        if check_raw is not None:
            if isinstance(check_raw, list):
                check_str = "\n".join(str(x).strip() for x in check_raw if str(x).strip())
            else:
                check_str = str(check_raw).strip()
            if check_str:
                check = _expand_variables(check_str, variables, f"Profile '{name}' check:")

        return cls(
            name=name,
            nspawn=effective,
            cmd=cmd,
            cmd_user=cmd_user,
            steps=effective_steps,
            parent=parent_name,
            local_steps=local_steps,
            check=check,
        )

    @property
    def fingerprint(self) -> str:
        parts = []
        for step in self.steps:
            parts.append(step.cmd or "")
            parts.append(step.user)
        parts.append("\n".join(self.nspawn))
        parts.append(self.check or "")
        # cmd: / cmd(user): participate in the fingerprint: changing the
        # post-build command (or its user) yields a different final image name.
        parts.append(self.cmd if isinstance(self.cmd, str) else "\n".join(self.cmd or []))
        parts.append(self.cmd_user)
        content = "\n---\n".join(parts).encode("utf-8")
        return hashlib.sha1(content).hexdigest()


@dataclass(frozen=True)
class BaseSpec:
    name: str
    create_cmd: str | None = None
    effective_name: str = ""
    nspawn_add: List[str] = field(default_factory=list)

    @classmethod
    def from_data(cls, data: Any, variables: dict[str, str] | None = None) -> "BaseSpec":
        if variables is None:
            variables = {}

        if isinstance(data, str):
            name = data.strip()
            if not name:
                raise ValueError("base cannot be empty")
            _validate_container_name("base", name)
            return cls(name=name, effective_name=name)

        if isinstance(data, dict):
            name = data.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                raise ValueError("base.name must be a non-empty string")

            name = name.strip()
            _validate_container_name("base", name)
            create_raw = data.get("create")
            create_cmd = None
            effective_name = name

            if create_raw:
                cmd_str = "\n".join(create_raw) if isinstance(create_raw, list) else str(create_raw)
                create_cmd_template = cmd_str.strip() if cmd_str.strip() else None

                if create_cmd_template:
                    expanded = _expand_variables(
                        create_cmd_template, variables, f"Base '{name}' create:"
                    )
                    h = hashlib.sha1(expanded.encode("utf-8")).hexdigest()[:16]
                    effective_name = f"{name}-{h}"
                    create_cmd = expanded
                else:
                    create_cmd = None
                    effective_name = name
            else:
                create_cmd = None
                effective_name = name

            add_raw = data.get("add", [])
            if not isinstance(add_raw, list):
                raise ValueError("base.add must be a list")
            nspawn_add = [str(item).strip() for item in add_raw if str(item).strip()]

            return cls(
                name=name,
                create_cmd=create_cmd,
                effective_name=effective_name,
                nspawn_add=nspawn_add,
            )

        raise ValueError("base must be a string or dict with 'name' key")


@dataclass(frozen=True)
class Step:
    index: int
    raw: Dict[str, Any]
    cmd: str | None = None
    user: str = "root"

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], index: int,
        variables: dict[str, str], profile_name: str,
        snippets: Dict[str, str] | None = None,
    ) -> "Step":
        if snippets is None:
            snippets = {}

        if not isinstance(data, dict) or len(data) != 1:
            if isinstance(data, str) and re.match(r"\s*(RUN|USE)\b[^:]*:", data):
                raise ValueError(
                    f"Profile '{profile_name}' step {index}: got the whole step line as a single string: '{data.strip()}'."
                    f" A step must be a YAML mapping, e.g. '- RUN(username): |' followed by the command lines."
                    f" (This usually means the entire line was wrapped in one pair of quotes.)"
                )
            return cls(index=index, raw=data)

        raw_key = next(iter(data.keys()))
        if isinstance(raw_key, str) and re.fullmatch(r"\s*(RUN|USE)\s*\(\s*\)\s*", raw_key):
            raise ValueError(
                f"Profile '{profile_name}' step {index}: step key '{raw_key}' has empty parens."
                f" Either drop the parens (plain 'RUN'/'USE' runs as root), or name the user, e.g. 'RUN(username)'."
            )
        cmd_type, user_raw = _parse_step_key(raw_key)
        value = data[raw_key]

        user: str = "root"
        if user_raw:
            user = _expand_variables(
                user_raw, variables, f"Profile '{profile_name}' step {index} user"
            ).strip()
            if not user:
                user = "root"

        if cmd_type == "RUN":
            raw_cmd = value
            cmd_str = "\n".join(raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)
            if cmd_str.strip():
                expanded = _expand_variables(
                    cmd_str, variables, f"Profile '{profile_name}' RUN step {index}"
                )
                return cls(index=index, raw=data, cmd=expanded.strip(), user=user)
            return cls(index=index, raw=data, cmd=None, user=user)

        elif cmd_type == "USE":
            snippet_name = str(value).strip()
            if snippet_name not in snippets:
                raise ValueError(
                    f"Profile '{profile_name}': Snippet '{snippet_name}' not found "
                    f"in top-level 'snippets:' section."
                )
            raw_cmd = snippets[snippet_name]
            if not raw_cmd:
                return cls(index=index, raw=data, cmd=None, user=user)

            expanded = _expand_variables(
                raw_cmd,
                variables,
                f"Snippet '{snippet_name}' (used in profile '{profile_name}')"
            )
            return cls(index=index, raw=data, cmd=expanded.strip(), user=user)

        return cls(index=index, raw=data, user=user)


@dataclass(frozen=True)
class Layer:
    path: Path
    hash: str

    @classmethod
    def initial(cls, base_path: Path, base_name: str) -> "Layer":
        initial_hash = hashlib.sha1(f"BASE:{base_name}".encode()).hexdigest()
        return cls(path=base_path, hash=initial_hash)


@dataclass
class Manifest:
    base: str
    yaml_file: str
    yaml_hash: str
    final_name: str
    profile: str
    nspawn_template: List[str]
    default_cmd: List[str] | str | None
    stage: str
    steps: int
    built_at: str
    logs: Dict[str, Dict[str, Any]]
    check: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fastcontainer": "1",
            "base": self.base,
            "yaml_file": self.yaml_file,
            "yaml_hash": self.yaml_hash,
            "final_name": self.final_name,
            "profile": self.profile,
            "nspawn_template": self.nspawn_template,
            "default_cmd": self.default_cmd,
            "check": self.check,
            "stage": self.stage,
            "steps": self.steps,
            "built_at": self.built_at,
            "logs": self.logs,
            "note": "This image was built with fastcontainer layered caching.",
        }

    @classmethod
    def from_spec(cls, spec: BuildSpec, profile: NspawnProfile, final_name: str,
                  completed_logs: Dict[str, Dict[str, Any]] | None = None,
                  stage: str = "final") -> "Manifest":
        if completed_logs is None:
            completed_logs = {}

        return cls(
            base=spec.base.name,
            yaml_file=spec.yaml_path.name,
            yaml_hash=spec.yaml_hash,
            final_name=final_name,
            profile=profile.name,
            nspawn_template=profile.nspawn[:],
            default_cmd=profile.cmd,
            check=profile.check,
            stage=stage,
            steps=len(completed_logs),
            built_at=datetime.now().isoformat(),
            logs=completed_logs,
        )

    @classmethod
    def from_subvolume(cls, path: Path) -> "Manifest":
        manifest_path = path / "fastcontainer.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"No fastcontainer.json found in {path}")
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            base=data["base"],
            yaml_file=data["yaml_file"],
            yaml_hash=data["yaml_hash"],
            final_name=data["final_name"],
            profile=data["profile"],
            nspawn_template=data["nspawn_template"],
            default_cmd=data.get("default_cmd"),
            check=data.get("check"),
            stage=data["stage"],
            steps=data["steps"],
            built_at=data["built_at"],
            logs=data["logs"],
        )

@dataclass(frozen=True)
class BuildSpec:
    base: BaseSpec
    yaml_path: Path
    yaml_hash: str
    profiles: Dict[str, NspawnProfile]
    snippets: Dict[str, str] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)   # declared env + defaults

    @classmethod
    def from_yaml(
        cls, yaml_path: Path, variables: dict[str, str] | None = None
    ) -> "BuildSpec":
        if variables is None:
            variables = {}

        if not yaml_path.is_file():
            raise FileNotFoundError(f"prepare.yaml not found at {yaml_path}")

        # === NEW: full recursive import-base support ===
        spec_raw = _resolve_yaml(yaml_path)

        # env handling (now fully merged from the whole import chain)
        local_env_raw = spec_raw.pop("env", {}) or {}
        if not isinstance(local_env_raw, dict):
            raise ValueError("env: must be a dictionary (key: default_value)")

        declared_env: Dict[str, str] = {}
        for k, v in local_env_raw.items():
            key = str(k).strip()
            if not key or not key.isidentifier():
                raise ValueError(f"Invalid env variable name: '{key}'")
            declared_env[key] = str(v).strip() if v is not None else ""

        # Validate that every -D flag actually exists in env:
        for key in variables:
            if key not in declared_env:
                raise ValueError(
                    f"Variable '{key}' defined on the command line with -D "
                    f"but it is not declared in any 'env:' section.\n"
                    f"Declared variables: {list(declared_env.keys()) or '(none)'}"
                )

        effective_variables = dict(declared_env)
        effective_variables.update(variables)

        # === NEW: support {{VAR}} inside env: values (chaining + any order + -D overrides) ===
        # Iterative expansion so VAR3 can reference VAR2 which references VAR1, etc.
        changed = True
        max_iterations = 20
        iteration = 0
        while changed and iteration < max_iterations:
            changed = False
            iteration += 1
            for k, v in list(effective_variables.items()):
                if isinstance(v, str) and '{{' in v:
                    try:
                        new_v = _expand_variables(v, effective_variables, f"env: {k}")
                        if new_v != v:
                            effective_variables[k] = new_v
                            changed = True
                    except ValueError as e:
                        raise ValueError(f"Error expanding env variable '{k}': {e}") from e
        if changed:
            raise ValueError(
                "Circular variable reference detected in 'env:' section "
                "(e.g. A references B and B references A)."
            )
        # Final safety check: any {{ left means unresolved cycle
        for k, v in list(effective_variables.items()):
            if isinstance(v, str) and '{{' in v:
                raise ValueError(
                    f"Circular or unresolved variable reference in env: '{k}' = {v!r}"
                )

        base_raw = spec_raw.get("base")
        if base_raw is None:
            raise ValueError("No 'base:' section found after resolving imports")

        base = BaseSpec.from_data(base_raw, variables=effective_variables)

        # snippets (already merged from chain)
        snippets: Dict[str, str] = {}
        local_snippets_raw = spec_raw.pop("snippets", {}) or {}
        if not isinstance(local_snippets_raw, dict):
            raise ValueError("snippets: must be a dictionary")

        for name, data in local_snippets_raw.items():
            if isinstance(data, dict) and "RUN" in data:
                cmd_raw = data["RUN"]
            else:
                cmd_raw = data
            cmd_str = "\n".join(cmd_raw) if isinstance(cmd_raw, list) else str(cmd_raw)
            snippets[name] = cmd_str.strip() if cmd_str.strip() else ""

        # profiles (already merged from chain)
        local_profiles_raw = spec_raw.pop("profiles", {}) or {}
        if not isinstance(local_profiles_raw, dict):
            raise ValueError("profiles: must be a dictionary")

        # Profile names become part of the final image directory name, so they
        # must be single path segments too (covers local AND imported profiles,
        # since the merge already happened).
        for pname in local_profiles_raw:
            # (No .strip(): the raw key is what ends up in the path, so the
            #  exact key must be a safe segment.)
            _validate_container_name("profile", str(pname))

        final_profiles_raw = local_profiles_raw   # the recursive merge already did the work

        resolved: Dict[str, NspawnProfile] = {}
        visiting: set[str] = set()

        def resolve_profile(name: str) -> NspawnProfile:
            if name in resolved:
                return resolved[name]
            if name not in final_profiles_raw:
                raise ValueError(f"Profile '{name}' not found in YAML (or imported base)")

            if name in visiting:
                raise ValueError(f"Circular dependency detected involving profile '{name}'")

            visiting.add(name)
            data = final_profiles_raw[name]
            extend_name = data.get("extend")

            if extend_name:
                if extend_name == name:
                    raise ValueError(f"Profile '{name}' cannot extend itself")
                resolve_profile(extend_name)

            profile = NspawnProfile.from_dict(
                name=name,
                data=data,
                resolved_profiles=resolved,
                base_nspawn=base.nspawn_add,
                variables=effective_variables,
                snippets=snippets,
            )
            resolved[name] = profile
            visiting.remove(name)
            return profile

        for name in list(final_profiles_raw.keys()):
            resolve_profile(name)

        yaml_hash = hashlib.sha1(yaml_path.read_bytes()).hexdigest()

        return cls(
            base=base,
            yaml_path=yaml_path,
            yaml_hash=yaml_hash,
            profiles=resolved,
            snippets=snippets,
            env=declared_env,
        )
