from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List

import yaml


@dataclass(frozen=True)
class NspawnProfile:
    """nspawn execution profile definition."""
    name: str
    nspawn: List[str]   # full command template containing {{ROOT}}

    @classmethod
    def from_data(cls, name: str, data: Any) -> "NspawnProfile":
        if not isinstance(data, dict) or "nspawn" not in data:
            raise ValueError(f"Profile '{name}' must contain 'nspawn:' key (list of strings)")
        nspawn_raw = data["nspawn"]
        if not isinstance(nspawn_raw, list):
            raise ValueError(f"Profile '{name}' nspawn must be a list of strings")
        return cls(
            name=name,
            nspawn=[str(item) for item in nspawn_raw]
        )


@dataclass(frozen=True)
class BaseSpec:
    """Base image specification - can be pre-existing or built via command."""
    name: str                    # user-friendly name (ubuntu-noble)
    create_cmd: str | None = None
    effective_name: str = ""     # actual subvolume name on disk (with hash if created)


    @classmethod
    def from_data(cls, data: Any) -> "BaseSpec":
        """Parse base: string or object with create command."""
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
                    # 16 char hash keeps names readable
                    h = hashlib.sha1(create_cmd.encode("utf-8")).hexdigest()[:16]
                    effective_name = f"{name}-{h}"

            return cls(
                name=name,
                create_cmd=create_cmd,
                effective_name=effective_name
            )

        raise ValueError("base must be a string or dict with 'name' key")


@dataclass(frozen=True)
class Step:
    """A single build step (currently only RUN is supported)."""
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
    """Represents one layer in the hash chain (used during build)."""
    path: Path
    hash: str

    @classmethod
    def initial(cls, base_path: Path, base_name: str, profile_name: str) -> "Layer":
        """Start the hash chain from the base subvolume, including the profile."""
        # Profile name is included so different nspawn flags produce different layer hashes
        initial_hash = hashlib.sha1(
            f"BASE:{base_name}:{profile_name}".encode()
        ).hexdigest()
        return cls(path=base_path, hash=initial_hash)

@dataclass
class Manifest:
    """Data written to /fastcontainer.json inside every layer and the final image."""
    base: str
    yaml_file: str
    yaml_hash: str
    final_name: str
    profile: str
    steps: int
    built_at: str
    logs: Dict[str, Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fastcontainer": "1",
            "base": self.base,
            "yaml_file": self.yaml_file,
            "yaml_hash": self.yaml_hash,
            "final_name": self.final_name,
            "profile": self.profile,
            "steps": self.steps,
            "built_at": datetime.now().isoformat(),
            "logs": self.logs,
            "note": "This image was built with fastcontainer layered caching.",
        }

    @classmethod
    def from_spec(cls, spec: BuildSpec, profile_name: str, completed_logs: Dict[str, Dict[str, Any]] | None = None) -> "Manifest":
        """Create manifest for a layer (partial logs) or final image (full logs)."""
        if completed_logs is None:
            completed_logs = {}

        return cls(
            base=spec.base.name,                    # friendly name (what the user wrote)
            yaml_file=spec.yaml_path.name,
            yaml_hash=spec.yaml_hash,
            final_name=spec.final_name,
            profile=profile_name,
            steps=len(completed_logs),
            built_at=datetime.now().isoformat(),
            logs=completed_logs,
        )


@dataclass(frozen=True)
class BuildSpec:
    """Complete build specification parsed from prepare.yaml."""
    base: BaseSpec
    steps: List[Step]
    yaml_path: Path
    yaml_hash: str
    final_name: str
    profiles: Dict[str, NspawnProfile]

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "BuildSpec":
        """Load and validate the YAML."""
        if not yaml_path.is_file():
            raise FileNotFoundError(f"prepare.yaml not found at {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)

        base_raw = spec.get("base")
        if base_raw is None:
            raise ValueError("YAML must contain 'base:'")

        base = BaseSpec.from_data(base_raw)
        steps_raw = spec.get("steps", [])

        if not isinstance(steps_raw, list):
            raise ValueError("'steps:' must be a list")

        # Profiles section is now required
        profiles_raw = spec.get("profiles")
        if not profiles_raw or not isinstance(profiles_raw, dict) or len(profiles_raw) == 0:
            raise ValueError("YAML must contain a non-empty 'profiles:' dictionary")

        profiles: Dict[str, NspawnProfile] = {}
        for name, data in profiles_raw.items():
            profiles[name] = NspawnProfile.from_data(name, data)

        # Deterministic final name
        yaml_hash = hashlib.sha1(yaml_path.read_bytes()).hexdigest()
        final_name = f"{base.effective_name}-{yaml_hash}"

        steps = [Step.from_dict(s, i + 1) for i, s in enumerate(steps_raw)]

        return cls(
            base=base,
            steps=steps,
            yaml_path=yaml_path,
            yaml_hash=yaml_hash,
            final_name=final_name,
            profiles=profiles,
        )
