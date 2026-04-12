from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List

import yaml


@dataclass(frozen=True)
class NspawnProfile:
    """nspawn execution profile definition (now with proper inheritance for build steps)."""
    name: str
    nspawn: List[str]           # full resolved list
    cmd: List[str] | str | None = None
    steps: List[Step] = field(default_factory=list)  # full effective (for manifest/logs)
    parent: str | None = None                    # NEW
    local_steps: List[Step] = field(default_factory=list)  # NEW - only the delta steps
    check: str | None = None

    @classmethod
    def from_dict(
        cls,
        name: str,
        data: dict,
        resolved_profiles: Dict[str, "NspawnProfile"],
        base_nspawn: List[str] | None = None,   # NEW: flags from base.add:
    ) -> "NspawnProfile":
        """Resolve a profile with extend + add/remove for flags AND steps."""
        extend_name = data.get("extend")
        add_raw = data.get("add", [])
        remove_raw = data.get("remove", data.get("del", []))
        cmd_raw = data.get("cmd")
        steps_raw = data.get("steps", [])

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
            # Root profile: start with systemd-nspawn + base.add: flags
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

        # === steps inheritance - now track local/delta steps ===
        parsed_local_steps: List[Step] = []
        for i, s in enumerate(steps_raw, 1):
            parsed_local_steps.append(Step.from_dict(s, i))

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

        # === cmd (unchanged) ===
        cmd: List[str] | str | None = None
        if cmd_raw is not None:
            if isinstance(cmd_raw, str):
                cmd_str = cmd_raw.strip()
                cmd = cmd_str if cmd_str else None
            elif isinstance(cmd_raw, list):
                cmd_list = [str(x) for x in cmd_raw if str(x).strip()]
                cmd = cmd_list if cmd_list else None
            else:
                raise ValueError(f"Profile '{name}' cmd: must be a string or list")

        # === check (NEW) ===
        check_raw = data.get("check")
        check: str | None = None
        if check_raw is not None:
            if isinstance(check_raw, list):
                check_str = "\n".join(str(x).strip() for x in check_raw if str(x).strip())
                check = check_str if check_str else None
            else:
                check_str = str(check_raw).strip()
                check = check_str if check_str else None

        return cls(
            name=name,
            nspawn=effective,
            cmd=cmd,
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
        # Final resolved nspawn flags (order matters for nspawn)
        parts.append("\n".join(self.nspawn))
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
    def from_data(cls, data: Any) -> "BaseSpec":
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
                create_cmd = cmd_str.strip()
                if create_cmd:
                    h = hashlib.sha1(create_cmd.encode("utf-8")).hexdigest()[:16]
                    effective_name = f"{name}-{h}"

            # NEW: support for base.add:
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

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int) -> "Step":
        if not isinstance(data, dict) or "RUN" not in data:
            return cls(index=index, raw=data)

        raw_cmd = data["RUN"]
        cmd_str = "\n".join(raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)
        return cls(index=index, raw=data, cmd=cmd_str.strip() if cmd_str else None)


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

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "BuildSpec":
        if not yaml_path.is_file():
            raise FileNotFoundError(f"prepare.yaml not found at {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)

        base_raw = spec.get("base")
        if base_raw is None:
            raise ValueError("YAML must contain 'base:'")

        base = BaseSpec.from_data(base_raw)

        profiles_raw = spec.get("profiles")
        if not profiles_raw or not isinstance(profiles_raw, dict) or len(profiles_raw) == 0:
            raise ValueError("YAML must contain a non-empty 'profiles:' dictionary")

        # Resolve profiles (flags + steps inheritance)
        resolved: Dict[str, NspawnProfile] = {}
        for name, data in profiles_raw.items():
            if not isinstance(data, dict):
                raise ValueError(f"Profile '{name}' must be a dictionary")
            resolved[name] = NspawnProfile.from_dict(
                name, data, resolved, base_nspawn=base.nspawn_add
            )

        # Final validation
        for p in resolved.values():
            if "{{ROOT}}" not in " ".join(p.nspawn):
                raise ValueError(
                    f"Profile '{p.name}' is missing the required '{{{{ROOT}}}}' "
                    "placeholder in flags"
                )

        yaml_hash = hashlib.sha1(yaml_path.read_bytes()).hexdigest()

        return cls(
            base=base,
            yaml_path=yaml_path,
            yaml_hash=yaml_hash,
            profiles=resolved,
        )
