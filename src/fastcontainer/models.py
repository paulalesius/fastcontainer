# fastcontainer/models.py
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List

import yaml

@dataclass(frozen=True)
class Step:
    """A single build step (currently only RUN is supported)."""
    index: int
    raw: Dict[str, Any]
    cmd: str | None = None  # normalized command string for RUN steps

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int) -> "Step":
        if not isinstance(data, dict) or "RUN" not in data:
            # Non-RUN steps are ignored for now (future-proof)
            return cls(index=index, raw=data)

        raw_cmd = data["RUN"]
        # Normalize exactly like the original script (preserves | block newlines)
        cmd_str = "\n".join(raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)

        return cls(index=index, raw=data, cmd=cmd_str.strip() if cmd_str else None)

@dataclass(frozen=True)
class BuildSpec:
    """Complete build specification parsed from prepare.yaml."""
    base: str
    steps: List[Step]
    yaml_path: Path
    yaml_hash: str
    final_name: str

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "BuildSpec":
        """Load and validate the YAML exactly as the original script did."""
        if not yaml_path.is_file():
            raise FileNotFoundError(f"prepare.yaml not found at {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)

        base_name = spec.get("base")
        steps_raw = spec.get("steps", [])

        if not base_name or not isinstance(steps_raw, list):
            raise ValueError("YAML must contain 'base:' (string) and 'steps:' (list)")

        # Deterministic final name (unchanged from original)
        yaml_hash = hashlib.sha1(yaml_path.read_bytes()).hexdigest()
        final_name = f"{base_name}-{yaml_hash}"

        # Convert raw steps to typed Step objects
        steps = [Step.from_dict(s, i + 1) for i, s in enumerate(steps_raw)]

        return cls(
            base=base_name,
            steps=steps,
            yaml_path=yaml_path,
            yaml_hash=yaml_hash,
            final_name=final_name,
        )

    def effective_steps(self) -> List[Step]:
        """Return only steps that actually do something (used for manifest)."""
        return [s for s in self.steps if s.cmd]

@dataclass
class Layer:
    """Represents one layer in the hash chain (used during build)."""
    path: Path
    hash: str

    @classmethod
    def initial(cls, base_path: Path, base_name: str) -> "Layer":
        """Start the hash chain from the base subvolume."""
        initial_hash = hashlib.sha1(f"BASE:{base_name}".encode()).hexdigest()
        return cls(path=base_path, hash=initial_hash)

@dataclass
class Manifest:
    """Data written to /fastcontainer.json inside the final image."""
    base: str
    yaml_file: str
    yaml_hash: str
    final_name: str
    steps: int
    built_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fastcontainer": "1",
            "base": self.base,
            "yaml_file": self.yaml_file,
            "yaml_hash": self.yaml_hash,
            "final_name": self.final_name,
            "steps": self.steps,
            "built_at": self.built_at,
            "note": "This image was built with fastcontainer layered caching.",
        }

    @classmethod
    def from_spec(cls, spec: BuildSpec) -> "Manifest":
        return cls(
            base=spec.base,
            yaml_file=spec.yaml_path.name,
            yaml_hash=spec.yaml_hash,
            final_name=spec.final_name,
            steps=len(spec.effective_steps()),
            built_at=datetime.now().isoformat(),
        )
