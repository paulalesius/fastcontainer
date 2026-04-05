# fastcontainer/builder.py
import hashlib
import uuid
import sys
import json
from pathlib import Path

from .models import BuildSpec, Layer, Manifest, Step
from .btrfs import snapshot, delete
from .nspawn import execute

import logging
logger = logging.getLogger("fastcontainer")


class Builder:
    """High-level orchestration of the layered btrfs build process."""

    def __init__(self, containers_dir: Path, spec: BuildSpec, quiet: bool = False, logger: logging.Logger | None = None):
        self.containers_dir = containers_dir.resolve()
        self.spec = spec
        self.final_path = self.containers_dir / spec.final_name
        self.quiet = quiet
        self.logger = logger or logging.getLogger("fastcontainer")

    # ... _layer_path stays exactly the same ...

    def _layer_path(self, step_hash: str) -> Path:
        return self.containers_dir / f"__{self.spec.base}-{step_hash}"

    def _build_layer(self, previous: Layer, step: Step) -> Layer:
        if not step.cmd:
            return previous

        content = previous.hash.encode() + step.cmd.encode("utf-8")
        step_hash = hashlib.sha1(content).hexdigest()
        layer_path = self._layer_path(step_hash)

        if layer_path.is_dir():
            self.logger.info(f"✅ Cache hit step {step.index}: {layer_path.name}")
            return Layer(path=layer_path, hash=step_hash)

        self.logger.info(f"\n[Step {step.index}/{len(self.spec.steps)}] RUN → new layer {layer_path.name}")

        temp_name = f"_{self.spec.base}-temp-{uuid.uuid4().hex[:8]}"
        temp_path = self.containers_dir / temp_name

        self.logger.info(f"  Creating temp snapshot → {temp_name}")
        snapshot(previous.path, temp_path, quiet=self.quiet)

        execute(temp_path, step.cmd, quiet=self.quiet)

        snapshot(temp_path, layer_path, quiet=self.quiet)
        delete(temp_path, quiet=self.quiet)

        self.logger.info(f"  ✓ Layer {layer_path.name} created")
        return Layer(path=layer_path, hash=step_hash)

    def build(self) -> None:
        if self.final_path.is_dir():
            self.logger.info(f"✅ {self.spec.final_name} already exists. Nothing to do.")
            return

        self.logger.info(f"Building layered image {self.spec.base} → {self.spec.final_name}")

        current = Layer.initial(
            base_path=self.containers_dir / self.spec.base,
            base_name=self.spec.base,
        )

        try:
            for step in self.spec.steps:
                try:
                    current = self._build_layer(current, step)
                except Exception as e:
                    self.logger.error(f"❌ Build failed at step {step.index}.")
                    self.logger.info("   Previous layers are cached and safe.")
                    self.logger.info("   Temporary volume (if any) kept for debugging.")
                    raise
        except Exception:
            raise
        else:
            self.logger.info(f"\nAll steps complete. Creating final image {self.spec.final_name}")

            final_temp_name = f"_{self.spec.base}-final-{uuid.uuid4().hex[:8]}"
            final_temp_path = self.containers_dir / final_temp_name

            snapshot(current.path, final_temp_path, quiet=self.quiet)

            manifest = Manifest.from_spec(self.spec)
            manifest_path = final_temp_path / "fastcontainer.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest.to_dict(), f, indent=2)
            self.logger.info(f"  Wrote manifest → {manifest_path}")

            snapshot(final_temp_path, self.final_path, quiet=self.quiet)
            delete(final_temp_path, quiet=self.quiet)

            self._prune_intermediates()

            self.logger.info(f"✅ Successfully built: {self.spec.final_name}")
            self.logger.info(f"   (Intermediates __{self.spec.base}-* were pruned on success)")

    def _prune_intermediates(self) -> None:
        self.logger.info("\n🧹 Pruning all intermediate layers (keeping only the final image)...")
        prefix = f"__{self.spec.base}-"
        for p in sorted(self.containers_dir.iterdir()):
            if (p.is_dir()
                and p.name.startswith(prefix)
                and len(p.name) == len(prefix) + 40):
                if all(c in "0123456789abcdef" for c in p.name[len(prefix):]):
                    self.logger.info(f"   Deleting {p.name}")
                    delete(p, commit=False, quiet=self.quiet)
        self.logger.info("   ✓ All intermediates removed")
