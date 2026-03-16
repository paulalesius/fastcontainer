#!/usr/bin/env python3
"""
fastcontainer - Minimal & safe btrfs + systemd-nspawn builder

Usage (exactly as you asked):
    ./fastcontainer.py <containers_dir> <base_name>

Example:
    ./fastcontainer.py /disk/container ubuntu-noble

This does ONLY what your original bash script did, nothing more:
- Snapshot → run PREPARE.sh → snapshot final → delete temp
- Same nspawn flags, same hash-of-PREPARE.sh naming
- Temp always stays on failure (exact same behaviour as your && chain)
- No extra flags, no read-only, no --force, no verbose
"""

import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path


def run(cmd):
    """Print and run (no shell, no injection risk)"""
    print("→", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True)


if len(sys.argv) != 3:
    print("Usage: fastcontainer.py <containers_dir> <base_name>")
    print("Example: fastcontainer.py /disk/container ubuntu-noble")
    sys.exit(1)

containers_dir = Path(sys.argv[1]).resolve()
base_name = sys.argv[2]

base_path = containers_dir / base_name
prepare_script = base_path / "PREPARE.sh"

# Safety: never allow path to escape the containers directory
if not base_path.resolve().is_relative_to(containers_dir):
    print("ERROR: base path must stay inside containers directory")
    sys.exit(1)

if not prepare_script.is_file():
    print(f"ERROR: PREPARE.sh not found at {prepare_script}")
    sys.exit(1)

# Auto re-execute with sudo (single password prompt)
if os.geteuid() != 0:
    print("→ Re-executing with sudo...")
    os.execvp("sudo", ["sudo", sys.executable, *sys.argv])

# Hash exactly like your original bash script
with open(prepare_script, "rb") as f:
    prepare_hash = hashlib.sha1(f.read()).hexdigest()

final_name = f"{base_name}-{prepare_hash}"
final_path = containers_dir / final_name
temp_name = f"_{base_name}-{uuid.uuid4().hex[:12]}"
temp_path = containers_dir / temp_name

print(f"Building {base_name} → {final_name}")

try:
    # 1. Temporary snapshot
    run(["btrfs", "subvolume", "snapshot", str(base_path), str(temp_path)])

    # 2. Run PREPARE.sh (exact same flags as your original script)
    run([
        "systemd-nspawn", "-D", str(temp_path),
        "--tmpfs=/var/tmp",
        "--private-users=no",
        "--resolv-conf=replace-stub",
        "/bin/bash", "/PREPARE.sh"
    ])

    # 3. Final snapshot
    print(f"Saving {temp_name} -> {final_name}")
    run(["btrfs", "subvolume", "snapshot", str(temp_path), str(final_path)])

    # 4. Delete temp (only reaches here on full success — same as your bash && chain)
    print(f"Deleting preparation volume {temp_name}")
    run(["btrfs", "subvolume", "delete", "-c", str(temp_path)])

    print(f"✅ Successfully built image: {final_name}")

except subprocess.CalledProcessError as e:
    print(f"❌ Failed: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Unexpected error: {e}")
    sys.exit(1)
