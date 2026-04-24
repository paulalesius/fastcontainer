import hashlib
import uuid
import json
from pathlib import Path
from typing import Any, List

from .models import BuildSpec, Layer, Manifest, Step, NspawnProfile
from .btrfs import snapshot, delete, create
from .nspawn import execute, exec_in_container, check_in_container
from .utils import run_and_capture

import logging
logger = logging.getLogger("fastcontainer")


class Builder:
    """High-level orchestration of the layered btrfs build process."""

    def __init__(self, containers_dir: Path, spec: BuildSpec, profile: NspawnProfile,
                 prune: bool = False, verbose: bool = False, logger: logging.Logger | None = None,
                 post_build_cmd: List[str] | str | None = None,
                 run_cmd: bool = True, shell: bool = False):
        self.containers_dir = containers_dir.resolve()
        self.spec = spec
        self.profile = profile
        self.prune = prune
        self.verbose = verbose
        self.logger = logger or logging.getLogger("fastcontainer")
        self.post_build_cmd = post_build_cmd
        self.run_cmd = run_cmd
        self.shell = shell

        self.cmd_user = getattr(profile, 'cmd_user', 'root')

        self.final_name = (
            f"{spec.base.effective_name}-{profile.name}-{profile.fingerprint}"
        )
        self.final_path = self.containers_dir / self.final_name

    def _run_post_build(self, cmd: List[str] | str | None) -> None:
        """Run post-build command (always shows output)."""
        if not cmd:
            return
        # Post-build cmd: always ephemeral (changes are thrown away)
        self.logger.info(f"Running profile command (cmd({self.cmd_user}))")
        exec_in_container(
            root=self.final_path,
            command=cmd,
            nspawn_template=self.profile.nspawn,
            user=self.cmd_user,
            ephemeral=True,
        )
        self.logger.info("Post-build command finished")

    def _handle_success(self) -> None:
        """Either run the normal post-build command OR drop into an interactive shell
        (when -s/--shell was passed). Parent profiles never get the shell."""
        cmd_to_run = self._get_cmd_to_run()
        if cmd_to_run:
            self.logger.info(
                f"→ Running profile command (as {self.cmd_user})"
            )
        if self.shell and self.run_cmd:
            self.logger.info("\n" + "═" * 80)
            self.logger.info("✅ BUILD SUCCESSFUL — Dropping into interactive shell")
            self.logger.info(f"   Final container: {self.final_path}")
            self.logger.info("   Type 'exit' (or Ctrl+D) when done.")
            self.logger.info("═" * 80 + "\n")

            try:
                exec_in_container(
                    root=self.final_path,
                    command=["/bin/bash", "-l"],
                    nspawn_template=self.profile.nspawn,
                    quiet=False,
                    check=False,
                    user=self.cmd_user,
                )
            except Exception as shell_err:
                self.logger.warning(f"Shell session had an error: {shell_err}")
        else:
            self._run_post_build(self._get_cmd_to_run())

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

        # === Improved preview for long/multi-line steps ===
        def _preview(step: Step) -> str:
            if not step.cmd:
                return "no-op"
            lines = [line.strip() for line in step.cmd.strip().splitlines() if line.strip()]
            if not lines:
                return "no-op"

            preview = lines[0]
            user_part = f" ({step.user})" if step.user != "root" else ""
            # Determine step type from the raw key (more reliable)
            step_type = "RUN" if any(k.startswith("RUN") for k in step.raw) else "USE"

            if len(lines) > 1 or len(preview) > 75:
                preview = preview[:72] + "..."
            return f"{step_type}{user_part}: {preview}"

        nspawn_context = "\n".join(self.profile.nspawn)
        content = (
            previous.hash.encode()
            + step.cmd.encode("utf-8")
            + nspawn_context.encode("utf-8")
        )
        step_hash = hashlib.sha1(content).hexdigest()
        layer_path = self._layer_path(step_hash)

        nice_preview = _preview(step)

        if layer_path.is_dir():
            self.logger.info(f"Step {step.index}/{total_steps} (cached) {nice_preview}")
            return Layer(path=layer_path, hash=step_hash)

        self.logger.info(f"Step {step.index}/{total_steps} {nice_preview}")

        temp_name = f"_{self.spec.base.effective_name}-temp-{uuid.uuid4().hex}"
        temp_path = self.containers_dir / temp_name

        try:
            snapshot(previous.path, temp_path)

            output = execute(temp_path, step.cmd, self.profile.nspawn, user=step.user, verbose=self.verbose)

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

        except Exception as e:  # CalledProcessError from a failing RUN step
            if self.shell and temp_path.is_dir():
                self.logger.info("\n" + "═" * 80)
                self.logger.info("💡 BUILD STEP FAILED — Dropping into interactive debug shell")
                self.logger.info(f"   Temporary layer: {temp_path}")
                self.logger.info("   You can inspect files, run commands, fix things, etc.")
                self.logger.info("   Type 'exit' (or Ctrl+D) when done. Build will still fail afterwards.")
                self.logger.info("═" * 80 + "\n")

                try:
                    exec_in_container(
                        root=temp_path,
                        command=["/bin/bash", "-l"],
                        nspawn_template=self.profile.nspawn,
                        quiet=False,
                        check=False,
                        user=step.user, # Run debug shell as the user of the failing build step
                    )
                except Exception as shell_err:
                    self.logger.warning(f"Shell session had an error: {shell_err}")

                self.logger.info("Shell exited — continuing with cleanup and build failure.")

            # Re-raise so the outer build() handler and normal error flow still work
            raise

        finally:
            if temp_path.is_dir():
                delete(temp_path)

    def _get_cmd_to_run(self) -> List[str] | str | None:
        """Only the leaf profile (or explicit CLI post-command) runs a cmd:.
        Parents (run_cmd=False) never execute any post-build command.
        """
        if not self.run_cmd:
            return None
        return self.post_build_cmd if self.post_build_cmd is not None else self.profile.cmd

    def _ensure_parent_built(self) -> None:
        """Recursively ensure the extended parent profile is built."""
        if not self.profile.parent:
            return
        parent_profile = self.spec.profiles[self.profile.parent]
        self.logger.info(f"Building parent profile: {parent_profile.name}")
        parent_builder = Builder(
            containers_dir=self.containers_dir,
            spec=self.spec,
            profile=parent_profile,
            prune=self.prune,
            verbose=self.verbose,
            logger=self.logger,
            post_build_cmd=None,
            run_cmd=False,
            shell=self.shell,
        )
        # no cmd_user needed for parents (we only care about it in leaf profiles)
        parent_builder.build()
        self.logger.info(f"Parent '{parent_profile.name}' ready")

    def build(self) -> None:
        if self.final_path.is_dir():
            if self.profile.check:
                self.logger.info(f"Image exists: {self.final_name} - running check")
                if check_in_container(
                    root=self.final_path,
                    command=self.profile.check,
                    nspawn_template=self.profile.nspawn,
                    verbose=self.verbose,
                ):
                    self.logger.info("Check passed - using cached image")
                    self._handle_success()
                    return
                else:
                    self.logger.warning("Check failed - deleting cache and forcing rebuild")
                    delete(self.final_path)

            else:
                self.logger.info(f"Image already exists: {self.final_name}")
                self._handle_success()
                return

        self.logger.info(f"Building profile: {self.profile.name}")
        if self.profile.parent:
            self.logger.info(f"  extends: {self.profile.parent}")

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

            self._handle_success()

    def _prune_intermediates(self) -> None:
        self.logger.info("Pruning intermediate layers...")
        prefix = f"__{self.spec.base.effective_name}-"
        for p in sorted(self.containers_dir.iterdir()):
            if (p.is_dir() and p.name.startswith(prefix) and len(p.name) == len(prefix) + 40):
                if all(c in "0123456789abcdef" for c in p.name[len(prefix):]):
                    delete(p)
        self.logger.info("Intermediate layers pruned")
