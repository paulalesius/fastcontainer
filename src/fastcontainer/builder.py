import hashlib
import uuid
import json
from pathlib import Path
from typing import Any, List

from .models import BuildSpec, Layer, Manifest, Step, NspawnProfile
from .btrfs import snapshot, delete, create
from .nspawn import execute, exec_in_container
from .utils import run_and_capture

import logging
logger = logging.getLogger("fastcontainer")


class Builder:
    """High-level orchestration of the layered btrfs build process."""

    def __init__(self, containers_dir: Path, spec: BuildSpec, profile: NspawnProfile,
                 prune: bool = False, verbose: bool = False, logger: logging.Logger | None = None,
                 post_build_cmd: List[str] | str | None = None):
        self.containers_dir = containers_dir.resolve()
        self.spec = spec
        self.profile = profile
        self.prune = prune
        self.verbose = verbose
        self.logger = logger or logging.getLogger("fastcontainer")
        self.post_build_cmd = post_build_cmd

        self.final_name = (
            f"{spec.base.effective_name}-{profile.name}-{profile.fingerprint}"
        )
        self.final_path = self.containers_dir / self.final_name

    def _run_post_build(self, cmd: List[str] | str | None) -> None:
        """Run post-build command (always shows output)."""
        if not cmd:
            return
        self.logger.info("Running post-build command")
        exec_in_container(
            root=self.final_path,
            command=cmd,
            nspawn_template=self.profile.nspawn,
        )
        self.logger.info("Post-build command finished")

    def _ensure_base_exists(self) -> None:
        """Create base subvolume from command if it doesn't exist."""
        base_path = self.containers_dir / self.spec.base.effective_name

        if base_path.is_dir():
            self.logger.info(f"Using existing base: {self.spec.base.effective_name}")
            return

        if not self.spec.base.create_cmd:
            raise FileNotFoundError(f"Base subvolume not found: {base_path}")

        self.logger.info(f"Creating base '{self.spec.base.effective_name}'...")

        temp_name = f"_{self.spec.base.name}-create-{uuid.uuid4().hex}"
        temp_path = self.containers_dir / temp_name

        try:
            create(temp_path)
            cmd = ["/bin/bash", "-c", self.spec.base.create_cmd]
            run_and_capture(cmd, verbose=self.verbose, cwd=temp_path)
            snapshot(temp_path, base_path)
            self.logger.info(f"Base {self.spec.base.effective_name} created successfully")
        except Exception:
            self.logger.error("Base creation failed")
            raise
        finally:
            if temp_path.is_dir():
                delete(temp_path)

    def _layer_path(self, step_hash: str) -> Path:
        return self.containers_dir / f"__{self.spec.base.effective_name}-{step_hash}"

    def _build_layer(self, previous: Layer, step: Step, current_logs: dict[str, dict[str, Any]], total_steps: int) -> Layer:
        """Build one layer (or use cache)."""
        if not step.cmd:
            return previous

        nspawn_context = "\n".join(self.profile.nspawn)
        content = (
            previous.hash.encode()
            + step.cmd.encode("utf-8")
            + nspawn_context.encode("utf-8")
        )
        step_hash = hashlib.sha1(content).hexdigest()
        layer_path = self._layer_path(step_hash)

        preview = (step.cmd or "-").splitlines()[0]

        if layer_path.is_dir():
            self.logger.info(f"Step {step.index}/{total_steps} (cached): {preview}")
            return Layer(path=layer_path, hash=step_hash)

        self.logger.info(f"Step {step.index}/{total_steps}: {preview}")

        temp_name = f"_{self.spec.base.effective_name}-temp-{uuid.uuid4().hex}"
        temp_path = self.containers_dir / temp_name

        try:
            snapshot(previous.path, temp_path)

            output = execute(temp_path, step.cmd, self.profile.nspawn, verbose=self.verbose)

            current_logs[f"{step.index:03d}"] = {
                "command": step.cmd,
                "output": output.splitlines()
            }

            # Write per-layer manifest
            manifest = Manifest.from_spec(
                self.spec,
                profile=self.profile,
                final_name=self.final_name,
                completed_logs=dict(current_logs),
                stage="intermediate"
            )
            manifest_path = temp_path / "fastcontainer.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest.to_dict(), f, indent=2)

            snapshot(temp_path, layer_path)
            return Layer(path=layer_path, hash=step_hash)
        finally:
            if temp_path.is_dir():
                delete(temp_path)

    def _ensure_parent_built(self) -> None:
        """Recursively ensure the extended parent profile is built."""
        if not self.profile.parent:
            return
        parent_profile = self.spec.profiles[self.profile.parent]
        self.logger.info(f"Building parent profile: {parent_profile.name} first")
        parent_builder = Builder(
            containers_dir=self.containers_dir,
            spec=self.spec,
            profile=parent_profile,
            prune=self.prune,
            verbose=self.verbose,
            logger=self.logger,
            post_build_cmd=None,
        )
        parent_builder.build()
        self.logger.info(f"Parent '{parent_profile.name}' ready")

    def build(self) -> None:
        if self.final_path.is_dir():
            self.logger.info(f"Image already exists: {self.final_name}")
            cmd_to_run = self.post_build_cmd if self.post_build_cmd is not None else self.profile.cmd
            self._run_post_build(cmd_to_run)
            return

        self.logger.info(f"Building profile: {self.profile.name}")
        if self.profile.parent:
            self.logger.info(f"  (extends {self.profile.parent})")

        self._ensure_base_exists()

        if self.profile.parent:
            self._ensure_parent_built()
            parent_profile = self.spec.profiles[self.profile.parent]
            parent_final_name = f"{self.spec.base.effective_name}-{parent_profile.name}-{parent_profile.fingerprint}"
            parent_final_path = self.containers_dir / parent_final_name
            current = Layer(path=parent_final_path, hash=parent_profile.fingerprint)
            self.logger.info(f"Starting delta build for '{self.profile.name}'")
        else:
            current = Layer.initial(
                base_path=self.containers_dir / self.spec.base.effective_name,
                base_name=self.spec.base.effective_name,
            )

        self.logger.info(f"Delta steps: {len(self.profile.local_steps)}\n")

        step_logs: dict[str, dict[str, Any]] = {}
        total_steps = len(self.profile.local_steps)
        current_step = None

        try:
            for step in self.profile.local_steps:
                current_step = step
                current = self._build_layer(current, step, step_logs, total_steps)
        except Exception as e:
            step_info = f"step {current_step.index}" if current_step is not None else "unknown step"
            self.logger.error(f"Build failed at {step_info} in profile '{self.profile.name}'")
            raise
        else:
            self.logger.info("All steps completed successfully. Creating final image...")

            final_temp_name = f"_{self.spec.base.effective_name}-final-{uuid.uuid4().hex}"
            final_temp_path = self.containers_dir / final_temp_name

            snapshot(current.path, final_temp_path)

            manifest = Manifest.from_spec(
                self.spec,
                profile=self.profile,
                final_name=self.final_name,
                completed_logs=step_logs,
                stage="final"
            )
            manifest_path = final_temp_path / "fastcontainer.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest.to_dict(), f, indent=2)

            snapshot(final_temp_path, self.final_path)
            delete(final_temp_path)

            if self.prune:
                self._prune_intermediates()
            else:
                self.logger.info("Intermediate layers kept")

            self.logger.info(f"Successfully built: {self.final_name}")

            cmd_to_run = self.post_build_cmd if self.post_build_cmd is not None else self.profile.cmd
            self._run_post_build(cmd_to_run)

    def _prune_intermediates(self) -> None:
        self.logger.info("Pruning intermediate layers...")
        prefix = f"__{self.spec.base.effective_name}-"
        for p in sorted(self.containers_dir.iterdir()):
            if (p.is_dir() and p.name.startswith(prefix) and len(p.name) == len(prefix) + 40):
                if all(c in "0123456789abcdef" for c in p.name[len(prefix):]):
                    delete(p)
        self.logger.info("Intermediate layers pruned")
