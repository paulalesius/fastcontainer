from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, List

import yaml

def _expand_variables(text: str, variables: dict[str, str], context: str) -> str:
    """Expand ONLY {{VAR}} syntax. ROOT is no longer a user placeholder."""
    if not text or not isinstance(text, str):
        return text

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1).strip()
        if var_name not in variables:
            raise ValueError(
                f"{context}: Undefined variable '{{{{ {var_name} }}}}'.\n"
                f"Pass it on the command line with -D {var_name}=value"
            )
        return variables[var_name]

    return re.sub(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}', replacer, text)

def _load_imported_raw(yaml_path: Path, import_base_path: str) -> dict:
    """Load the imported library as raw YAML dict (not a full BuildSpec)."""
    if not import_base_path:
        raise ValueError("import-base cannot be empty")
    import_path = (yaml_path.parent / import_base_path).resolve()
    if not import_path.is_file():
        raise FileNotFoundError(f"Imported base YAML not found: {import_path}")
    with open(import_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _forbid_manual_directory(profile_name: str, flags: List[str]) -> None:
    """Completely forbid the user from specifying the root directory.
    fastcontainer now injects it automatically."""
    for i, item in enumerate(flags):
        flag = str(item).strip()
        if flag in ("-D", "--directory") or flag.startswith(("--directory=", "-D=")):
            raise ValueError(
                f"Profile '{profile_name}': Do NOT specify -D, --directory or any root path.\n"
                f"fastcontainer automatically adds '-D <root>' for you.\n"
                f"Remove any such lines from your 'add:' section."
            )
        # Catch the old two-line pattern users used to write
        if flag == "-D" and i + 1 < len(flags) and str(flags[i + 1]).strip() == "{{ROOT}}":
            raise ValueError(
                f"Profile '{profile_name}': Do NOT specify '-D' and '{{{{ROOT}}}}' anymore.\n"
                f"fastcontainer now injects the correct directory flag automatically."
            )

def _forbid_manual_user(profile_name: str, flags: List[str]) -> None:
    """Completely forbid --user / -u in add: (fastcontainer now controls it per RUN/USE)."""
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
    # Matches: RUN, RUN(root), RUN({{VAR}}), USE(noname), etc.
    match = re.match(r'^(RUN|USE)\s*(?:\(([^)]+)\))?$', key.strip())
    if not match:
        return key.strip(), None
    cmd_type = match.group(1)
    user = match.group(2).strip() if match.group(2) else None
    return cmd_type, user

@dataclass(frozen=True)
class NspawnProfile:
    """nspawn execution profile definition (now with proper inheritance for build steps)."""
    name: str
    nspawn: List[str]           # full resolved list
    cmd: List[str] | str | None = None
    cmd_user: str = "root"      # NEW: user for the final cmd:
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
        """Resolve a profile with extend + add/remove for flags AND steps.
        ONLY {{VAR}} syntax is supported (as requested).
        """
        if variables is None:
            variables = {}
        if snippets is None:
            snippets = {}

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

        # === nspawn flags ===
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

        # forbid --user
        _forbid_manual_user(name, effective)

        # === Expand ONLY {{VAR}} in add: flags ===
        effective = [
            _expand_variables(str(flag), variables, f"Profile '{name}' add:")
            for flag in effective
        ]

        # forbid any manual directory specification
        _forbid_manual_directory(name, effective)

        # === steps (ONLY {{VAR}}) ===
        parsed_local_steps: List[Step] = []
        for i, s in enumerate(steps_raw, 1):
            parsed_local_steps.append(
                Step.from_dict(
                    s, i, variables=variables, profile_name=name, snippets=snippets
                )
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

        # === cmd: support both "cmd:" and "cmd(user):" (like RUN(user)) ===
        cmd: List[str] | str | None = None
        cmd_user: str = "root"

        # Look for either a plain "cmd" key OR a "cmd(user)" key
        cmd_key = None
        cmd_value = None
        for k, v in list(data.items()):
            k_str = str(k).strip()
            if k_str == "cmd" or (k_str.startswith("cmd(") and k_str.endswith(")")):
                cmd_key = k_str
                cmd_value = v
                # Remove it from data so it doesn't leak into other processing
                data.pop(k, None)
                break

        if cmd_key is not None:
            # Extract user if it's cmd(someuser):
            if cmd_key != "cmd":
                match = re.search(r'cmd\(([^)]+)\)', cmd_key)
                if match:
                    user_raw = match.group(1).strip()
                    cmd_user = _expand_variables(
                        user_raw, variables, f"Profile '{name}' cmd: user"
                    ).strip() or "root"

            # Now process the value (string or list) exactly like before
            value = cmd_value
            if isinstance(value, str):
                cmd_str = value.strip()
                if cmd_str:
                    cmd_str = _expand_variables(
                        cmd_str, variables, f"Profile '{name}' cmd:"
                    )
                    cmd = cmd_str
            elif isinstance(value, list):
                cmd_list = [
                    _expand_variables(str(x).strip(), variables, f"Profile '{name}' cmd:")
                    for x in value if str(x).strip()
                ]
                cmd = cmd_list if cmd_list else None
            else:
                raise ValueError(
                    f"Profile '{name}' cmd: must be a string or list "
                    f"(or cmd(user): form)"
                )

        # === check: (ONLY {{VAR}}) ===
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
        """Stable content hash of what will actually be built."""
        parts = []
        # All resolved steps in order
        for step in self.steps:
            parts.append(step.cmd or "")
            parts.append(step.user)

        # Final resolved nspawn flags (order matters for nspawn)
        parts.append("\n".join(self.nspawn))
        # check: is now part of the image identity
        parts.append(self.check or "")
        content = "\n---\n".join(parts).encode("utf-8")
        return hashlib.sha1(content).hexdigest()


@dataclass(frozen=True)
class BaseSpec:
    """Base image specification - can be pre-existing or built via command.
    Now supports `add:` for default nspawn flags inherited by all profiles."""
    name: str
    create_cmd: str | None = None
    effective_name: str = ""
    nspawn_add: List[str] = field(default_factory=list)  # NEW

    @classmethod
    def from_data(cls, data: Any, variables: dict[str, str] | None = None) -> "BaseSpec":
        if isinstance(data, str):
            if not data.strip():
                raise ValueError("base cannot be empty")
            return cls(name=data.strip(), effective_name=data.strip())

        if isinstance(data, dict):
            name = data.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                raise ValueError("base.name must be a non-empty string")

            name = name.strip()
            create_raw = data.get("create")
            create_cmd = None
            effective_name = name

            if create_raw:
                cmd_str = "\n".join(create_raw) if isinstance(create_raw, list) else str(create_raw)
                create_cmd = cmd_str.strip() if cmd_str.strip() else None
                effective_name = name

                if create_cmd:
                    # hash is computed on the original (unexpanded) command → stable base name
                    h = hashlib.sha1(create_cmd.encode("utf-8")).hexdigest()[:16]
                    effective_name = f"{name}-{h}"
                    if variables:
                        create_cmd = _expand_variables(
                            create_cmd, variables, f"Base '{name}' create:"
                        )

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
    """A single build step (RUN or USE). Now supports per-step user."""
    index: int
    raw: Dict[str, Any]
    cmd: str | None = None
    user: str = "root"          # NEW: user that this step runs as

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], index: int,
        variables: dict[str, str], profile_name: str,
        snippets: Dict[str, str] | None = None,
    ) -> "Step":
        if snippets is None:
            snippets = {}

        if not isinstance(data, dict) or len(data) != 1:
            # Fallback for malformed steps
            return cls(index=index, raw=data)

        raw_key = next(iter(data.keys()))
        cmd_type, user_raw = _parse_step_key(raw_key)
        value = data[raw_key]

        # Resolve user (supports {{VAR}})
        user: str = "root"
        if user_raw:
            user = _expand_variables(
                user_raw, variables, f"Profile '{profile_name}' step {index} user"
            ).strip()
            if not user:
                user = "root"

        if cmd_type == "RUN":
            # ── original RUN handling ─────────────────────────
            raw_cmd = value
            cmd_str = "\n".join(raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)
            if cmd_str.strip():
                expanded = _expand_variables(
                    cmd_str, variables, f"Profile '{profile_name}' RUN step {index}"
                )
                return cls(index=index, raw=data, cmd=expanded.strip(), user=user)
            return cls(index=index, raw=data, cmd=None, user=user)

        elif cmd_type == "USE":
            # ── USE: snippet handling ─────────────────────────
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

        # Fallback
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

    @classmethod
    def from_yaml(
        cls, yaml_path: Path, variables: dict[str, str] | None = None
    ) -> "BuildSpec":
        if variables is None:
            variables = {}

        if not yaml_path.is_file():
            raise FileNotFoundError(f"prepare.yaml not found at {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            spec_raw = yaml.safe_load(f) or {}

        import_base = spec_raw.pop("import-base", None)
        base_raw = spec_raw.get("base")

        if import_base is not None and base_raw is not None:
            raise ValueError(
                f"YAML {yaml_path.name} contains both 'base:' and 'import-base:'. "
                "Only one is allowed."
            )

        if import_base is not None:
            # Load imported file as raw dict only
            imported_raw = _load_imported_raw(yaml_path, import_base)

            # Base must come from the imported file
            if "base" not in imported_raw:
                raise ValueError(f"Imported file {import_base} must contain a 'base:' section")
            base = BaseSpec.from_data(imported_raw["base"], variables=variables)

            # Snippets from import (will be overridden by local later)
            imported_snippets_raw = imported_raw.get("snippets", {})
            if not isinstance(imported_snippets_raw, dict):
                imported_snippets_raw = {}

            # Profiles from import (raw dicts)
            imported_profiles_raw = imported_raw.get("profiles", {})
            if not isinstance(imported_profiles_raw, dict):
                imported_profiles_raw = {}
        else:
            # Normal local base (no import)
            if base_raw is None:
                raise ValueError("YAML must contain either 'base:' or 'import-base:'")
            base = BaseSpec.from_data(base_raw, variables=variables)
            imported_snippets_raw = {}
            imported_profiles_raw = {}

        # === snippets: imported + local override ===
        snippets: Dict[str, str] = {}
        local_snippets_raw = spec_raw.pop("snippets", {}) or {}
        if not isinstance(local_snippets_raw, dict):
            raise ValueError("snippets: must be a dictionary")

        # Start with imported snippets
        for name, data in imported_snippets_raw.items():
            if isinstance(data, dict) and "RUN" in data:
                cmd_raw = data["RUN"]
            else:
                cmd_raw = data
            cmd_str = "\n".join(cmd_raw) if isinstance(cmd_raw, list) else str(cmd_raw)
            snippets[name] = cmd_str.strip() if cmd_str.strip() else ""

        # Local snippets override
        for name, data in local_snippets_raw.items():
            if isinstance(data, dict) and "RUN" in data:
                cmd_raw = data["RUN"]
            else:
                cmd_raw = data
            cmd_str = "\n".join(cmd_raw) if isinstance(cmd_raw, list) else str(cmd_raw)
            snippets[name] = cmd_str.strip() if cmd_str.strip() else ""

        # === profiles: imported + local override (raw dicts) ===
        local_profiles_raw = spec_raw.pop("profiles", {}) or {}
        if not isinstance(local_profiles_raw, dict):
            raise ValueError("profiles: must be a dictionary")

        final_profiles_raw = dict(imported_profiles_raw)  # start with imported
        final_profiles_raw.update(local_profiles_raw)     # local completely replaces

        # Now resolve all profiles (exactly like before)
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
            data = final_profiles_raw[name]          # ← always a dict now
            extend_name = data.get("extend")         # ← safe .get()

            if extend_name:
                if extend_name == name:
                    raise ValueError(f"Profile '{name}' cannot extend itself")
                resolve_profile(extend_name)

            profile = NspawnProfile.from_dict(
                name=name,
                data=data,                           # ← always dict
                resolved_profiles=resolved,
                base_nspawn=base.nspawn_add,
                variables=variables,
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
        )
