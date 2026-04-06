import hashlib
import uuid
import sys
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
                 prune: bool = False, quiet: bool = False, logger: logging.Logger | None = None,
                 post_build_cmd: List[str] | None = None):
        self.containers_dir = containers_dir.resolve()
        self.spec = spec
        self.profile = profile
        self.prune = prune
        self.quiet = quiet
        self.logger = logger or logging.getLogger("fastcontainer")
        self.post_build_cmd = post_build_cmd

        self.final_name = f"{spec.base.effective_name}-{profile.name}-{spec.yaml_hash}"
        self.final_path = self.containers_dir / self.final_name

    def _ensure_base_exists(self) -> None:
        """Create base subvolume from command if it doesn't exist (cached by hash)."""
        base_path = self.containers_dir / self.spec.base.effective_name

        if base_path.is_dir():
            self.logger.info(f"[OK] Using existing base: {self.spec.base.effective_name}")
            return

        if not self.spec.base.create_cmd:
            raise FileNotFoundError(f"Base subvolume not found: {base_path}")

        self.logger.info(f"[BUILD] Creating base '{self.spec.base.effective_name}' from command...")

        temp_name = f"_{self.spec.base.name}-create-{uuid.uuid4().hex}"
        temp_path = self.containers_dir / temp_name

        try:
            self.logger.info(f"  Creating empty subvolume → {temp_name}")
            create(temp_path, quiet=self.quiet)

            self.logger.info("  Executing base creation script (host, cwd = subvolume)...")
            cmd = ["/bin/bash", "-c", self.spec.base.create_cmd]
            run_and_capture(cmd, quiet=self.quiet, cwd=temp_path)

            self.logger.info(f"[OK] Base {self.spec.base.effective_name} created successfully")

            snapshot(temp_path, base_path, quiet=self.quiet)

        except Exception:
            self.logger.error("❌ Base creation failed — cleaning up temporary subvolume")
            raise
        finally:
            if temp_path.is_dir():
                delete(temp_path, quiet=True)

    def _layer_path(self, step_hash: str) -> Path:
        return self.containers_dir / f"__{self.spec.base.effective_name}-{step_hash}"

    def _build_layer(self, previous: Layer, step: Step, current_logs: dict[str, dict[str, Any]]) -> Layer:
        """Build one layer and update the logs dict (passed by reference)."""
        if not step.cmd:
            return previous

        content = previous.hash.encode() + step.cmd.encode("utf-8")
        step_hash = hashlib.sha1(content).hexdigest()
        layer_path = self._layer_path(step_hash)

        if layer_path.is_dir():
            # Pure content-based caching. Layers are profile-independent by design.
            # nspawn flags only affect how we *execute* the step, not the resulting filesystem
            # for typical RUN commands (apt, git, cmake, etc.).
            self.logger.info(f"✅ Cache hit step {step.index}: {layer_path.name}")
            return Layer(path=layer_path, hash=step_hash)

        self.logger.info(f"\n[Step {step.index}/{len(self.spec.steps)}] RUN -> new layer {layer_path.name}")
        self.logger.info(f"    Command:\n{step.cmd}")

        temp_name = f"_{self.spec.base.effective_name}-temp-{uuid.uuid4().hex}"
        temp_path = self.containers_dir / temp_name

        self.logger.info(f"  Creating temp snapshot -> {temp_name}")
        snapshot(previous.path, temp_path, quiet=self.quiet)

        self.logger.info(f"  Executing step {step.index}...")
        output = execute(temp_path, step.cmd, self.profile.nspawn, quiet=self.quiet)

        current_logs[f"{step.index:03d}"] = {
            "command": step.cmd,
            "output": output.splitlines()
        }
        self.logger.info(f"  [OK] Step {step.index} finished")

        # Write per-layer manifest (self-describing + clearly marked intermediate)
        manifest = Manifest.from_spec(
            self.spec,
            profile=self.profile,                    # ← fixed
            final_name=self.final_name,
            completed_logs=dict(current_logs),
            stage="intermediate"
        )
        manifest_path = temp_path / "fastcontainer.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)
        self.logger.info(f"  ✓ Wrote per-layer manifest → fastcontainer.json")

        snapshot(temp_path, layer_path, quiet=self.quiet)
        delete(temp_path, quiet=self.quiet)

        self.logger.info(f"  [OK] Layer {layer_path.name} created")
        return Layer(path=layer_path, hash=step_hash)

    def build(self) -> None:
        if self.final_path.is_dir():
            self.logger.info(f"✅ {self.final_name} already exists. Nothing to do.")

            # CLI override > profile.cmd  (same logic we already use after a fresh build)
            cmd_to_run = self.post_build_cmd or self.profile.cmd
            if cmd_to_run:
                self.logger.info(f"→ Running post-build command: {' '.join(cmd_to_run)}")
                exec_in_container(
                    root=self.final_path,
                    command=cmd_to_run,
                    nspawn_template=self.profile.nspawn,
                    quiet=self.quiet
                )
                self.logger.info("✅ Post-build command finished")
            return

        self.logger.info(f"Building layered image {self.spec.base.effective_name} → {self.final_name} (profile: {self.profile.name})")

        self._ensure_base_exists()

        current = Layer.initial(
            base_path=self.containers_dir / self.spec.base.effective_name,
            base_name=self.spec.base.effective_name,
        )

        step_logs: dict[str, dict[str, Any]] = {}

        try:
            for step in self.spec.steps:
                try:
                    current = self._build_layer(current, step, step_logs)
                except Exception as e:
                    self.logger.error(f"❌ Build failed at step {step.index}.")
                    self.logger.info("   Previous layers are cached and safe.")
                    raise
        except Exception:
            raise
        else:
            self.logger.info(f"\nAll steps complete. Creating final image {self.final_name}")

            final_temp_name = f"_{self.spec.base.effective_name}-final-{uuid.uuid4().hex}"
            final_temp_path = self.containers_dir / final_temp_name

            snapshot(current.path, final_temp_path, quiet=self.quiet)

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
            self.logger.info(f"  Wrote final manifest → {manifest_path}")

            snapshot(final_temp_path, self.final_path, quiet=self.quiet)
            delete(final_temp_path, quiet=self.quiet)

            if self.prune:
                self._prune_intermediates()
                self.logger.info(f"   (Intermediates __{self.spec.base.effective_name}-* were pruned on success)")
            else:
                self.logger.info(f"   (Intermediate layers kept for reuse by other builds)")

            self.logger.info(f"✅ Successfully built: {self.final_name}")

            # ── Run post-build command (CLI override > profile.cmd) ─────────────────
            cmd_to_run = self.post_build_cmd or self.profile.cmd
            if cmd_to_run:
                self.logger.info(f"→ Running post-build command: {' '.join(cmd_to_run)}")
                exec_in_container(
                    root=self.final_path,
                    command=cmd_to_run,
                    nspawn_template=self.profile.nspawn,
                    quiet=self.quiet
                )
                self.logger.info("✅ Post-build command finished")
            # ────────────────────────────────────────────────────────────────

    def _prune_intermediates(self) -> None:
        self.logger.info("\n🧹 Pruning all intermediate layers (keeping only the final image)...")
        prefix = f"__{self.spec.base.effective_name}-"
        for p in sorted(self.containers_dir.iterdir()):
            if (p.is_dir()
                and p.name.startswith(prefix)
                and len(p.name) == len(prefix) + 40):
                if all(c in "0123456789abcdef" for c in p.name[len(prefix):]):
                    self.logger.info(f"   Deleting {p.name}")
                    delete(p, quiet=self.quiet)
        self.logger.info("   ✓ All intermediates removed")
