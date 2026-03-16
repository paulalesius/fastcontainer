#!/usr/bin/env python3
"""
fastcontainer - Minimal & safe btrfs + systemd-nspawn builder (YAML version)

Usage:
    ./fastcontainer.py <containers_dir> <prepare.yaml>

Example:
    ./fastcontainer.py /disk/containers ./myapp/prepare.yaml
"""

import hashlib
import os
import subprocess
import sys
import uuid
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

# Load and validate YAML
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

# Safety: never escape the containers directory
if not base_path.resolve().is_relative_to(containers_dir):
    print("ERROR: base path must stay inside containers directory")
    sys.exit(1)

# Auto re-execute with sudo (single password prompt)
if os.geteuid() != 0:
    print("→ Re-executing with sudo...")
    os.execvp("sudo", ["sudo", sys.executable, *sys.argv])

# Hash the YAML file itself (same deterministic naming as your original PREPARE.sh)
yaml_hash = hashlib.sha1(yaml_path.read_bytes()).hexdigest()[:16]
final_name = f"{base_name}-{yaml_hash}"
final_path = containers_dir / final_name
temp_name = f"_{base_name}-{uuid.uuid4().hex[:12]}"
temp_path = containers_dir / temp_name

print(f"Building {base_name} → {final_name} (using {yaml_path.name})")

try:
    # 1. Temporary snapshot
    print(f"Creating temporary snapshot → {temp_name}")
    run(["btrfs", "subvolume", "snapshot", str(base_path), str(temp_path)])

    # 2. Run every step sequentially
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or "RUN" not in step:
            print(f"⚠️  Step {i} ignored (only RUN supported for now)")
            continue

        command = step["RUN"]
        if not command:
            continue

        print(f"\n[Step {i}/{len(steps)}] RUN")
        # Support both scalar and list form (flexible for future)
        cmd_str = " ".join(command) if isinstance(command, list) else str(command)

        run([
            "systemd-nspawn", "-D", str(temp_path),
            "--tmpfs=/var/tmp",
            "--private-users=no",
            "--resolv-conf=replace-stub",
            "/bin/bash", "-c", cmd_str
        ])

    # 3. Final snapshot
    print(f"\nSaving {temp_name} → {final_name}")
    run(["btrfs", "subvolume", "snapshot", str(temp_path), str(final_path)])

    # 4. Delete temp (only on full success)
    print(f"Deleting temporary volume {temp_name}")
    run(["btrfs", "subvolume", "delete", "-c", str(temp_path)])

    print(f"✅ Successfully built image: {final_name}")

except subprocess.CalledProcessError as e:
    print(f"❌ Build failed (temporary volume kept for debugging): {temp_path}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Unexpected error: {e}")
    sys.exit(1)
