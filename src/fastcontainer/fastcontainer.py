#!/usr/bin/env python3
"""
fastcontainer - Minimal & safe btrfs + systemd-nspawn builder (LAYERED VERSION)

Usage:
    ./fastcontainer.py <containers_dir> <prepare.yaml>

Example:
    ./fastcontainer.py /disk/containers ./nginx-prepare.yaml

What this now does (Docker-style layers with automatic caching):
- Every RUN becomes its own permanent snapshot (named _{base_name}-<hash>)
- If a layer already exists → instant cache hit, zero nspawn
- Failure at step 7 → steps 1-6 stay forever; next run resumes at step 7
- Final image still named exactly like before (base-yamlhash) so your workflows are unchanged
- One small JSON manifest is written into the final container root for introspection
- Base starts the hash chain cleanly (no "hash of directory name" issue)
"""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import yaml


def run(cmd):
    """Print and run (no shell, no injection risk)"""
    print("→", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True)


if len(sys.argv) != 3:
    print("Usage: fastcontainer.py <containers_dir> <prepare.yaml>")
    print("Example: fastcontainer.py /disk/containers ./nginx-prepare.yaml")
    sys.exit(1)

containers_dir = Path(sys.argv[1]).resolve()
yaml_path = Path(sys.argv[2]).resolve()

if not yaml_path.is_file():
    print(f"ERROR: prepare.yaml not found at {yaml_path}")
    sys.exit(1)

# Load YAML
with open(yaml_path, "r", encoding="utf-8") as f:
    spec = yaml.safe_load(f)

base_name = spec.get("base")
steps = spec.get("steps", [])

if not base_name or not isinstance(steps, list):
    print("ERROR: YAML must contain 'base:' and 'steps:' (list)")
    sys.exit(1)

base_path = containers_dir / base_name
if not base_path.is_dir():
    print(f"ERROR: Base subvolume not found: {base_path}")
    sys.exit(1)

if not base_path.resolve().is_relative_to(containers_dir):
    print("ERROR: base path must stay inside containers directory")
    sys.exit(1)

# Auto re-execute with sudo
if os.geteuid() != 0:
    print("ERROR: Root required")
    sys.exit(1)

# Deterministic final name (unchanged from your original design)
yaml_hash = hashlib.sha1(yaml_path.read_bytes()).hexdigest()[:16]
final_name = f"{base_name}-{yaml_hash}"
final_path = containers_dir / final_name

# Early exit if final image already exists (super fast)
if final_path.is_dir():
    print(f"✅ {final_name} already exists. Nothing to do.")
    sys.exit(0)

temp_name = f"_{base_name}-temp-{uuid.uuid4().hex[:8]}"
temp_path = containers_dir / temp_name

print(f"Building layered image {base_name} → {final_name}")

# Start the hash chain with the base (clean, no directory-name hash issues)
current_path = base_path
current_hash = hashlib.sha1(f"BASE:{base_name}".encode()).hexdigest()[:16]

try:
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or "RUN" not in step:
            print(f"⚠️  Step {i} ignored (only RUN supported)")
            continue

        raw_cmd = step["RUN"]
        # Normalize for hashing + execution (preserves newlines from | blocks)
        cmd_str = "\n".join(raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)

        if not cmd_str.strip():
            continue

        # Chained hash for this exact layer
        content = current_hash.encode() + cmd_str.encode("utf-8")
        step_hash = hashlib.sha1(content).hexdigest()[:16]
        layer_name = f"_{base_name}-{step_hash}"
        layer_path = containers_dir / layer_name

        # CACHE HIT?
        if layer_path.is_dir():
            print(f"✅ Cache hit step {i}: {layer_name}")
            current_path = layer_path
            current_hash = step_hash
            continue

        # No cache → build this layer
        print(f"\n[Step {i}/{len(steps)}] RUN → new layer {layer_name}")

        # Temporary snapshot from previous layer
        print(f"  Creating temp snapshot → {temp_name}")
        run(["btrfs", "subvolume", "snapshot", str(current_path), str(temp_path)])

        # Execute the command inside nspawn (exact same flags you had)
        # Execute in a login shell
        run([
            "systemd-nspawn", "-D", str(temp_path),
            "--tmpfs=/var/tmp",
            "--private-users=no",
            "--resolv-conf=replace-stub",
            "/bin/bash", "-l", "-c", cmd_str
        ])

        # Promote temp to permanent layer
        run(["btrfs", "subvolume", "snapshot", str(temp_path), str(layer_path)])

        # Delete temp immediately (only the current step temp stays on failure)
        run(["btrfs", "subvolume", "delete", "-c", str(temp_path)])

        # Update chain for next step
        current_path = layer_path
        current_hash = step_hash

    # ALL STEPS DONE → create clean final image with manifest
    print(f"\nAll steps complete. Creating final image {final_name}")

    final_temp_name = f"_{base_name}-final-{uuid.uuid4().hex[:8]}"
    final_temp_path = containers_dir / final_temp_name

    # Snapshot from last layer
    run(["btrfs", "subvolume", "snapshot", str(current_path), str(final_temp_path)])

    # Write simple manifest directly into the final container root
    manifest = {
        "fastcontainer": "1",
        "base": base_name,
        "yaml_file": yaml_path.name,
        "yaml_hash": yaml_hash,
        "final_name": final_name,
        "steps": len([s for s in steps if isinstance(s, dict) and "RUN" in s]),
        "built_at": datetime.now().isoformat(),
        "note": "This image was built with fastcontainer layered caching."
    }
    manifest_path = final_temp_path / "fastcontainer.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"  Wrote manifest → {manifest_path}")

    # Final clean snapshot (this is the one users will use)
    run(["btrfs", "subvolume", "snapshot", str(final_temp_path), str(final_path)])

    # Cleanup final temp
    run(["btrfs", "subvolume", "delete", "-c", str(final_temp_path)])

    print(f"✅ Successfully built: {final_name}")
    print(f"   (Intermediates are cached as _{base_name}-* for future rebuilds)")

except subprocess.CalledProcessError as e:
    print(f"❌ Build failed at current step.")
    print(f"   Temporary volume kept for debugging: {temp_path}")
    print(f"   Previous layers are safe and will be reused next run.")
    sys.exit(1)
except Exception as e:
    print(f"❌ Unexpected error: {e}")
    sys.exit(1)
