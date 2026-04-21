fastcontainer - Minimal btrfs + systemd-nspawn layered container builder
============================================================================

<div align="center">
  <img src="logo.jpg" alt="fastcontainer">
</div>

### Installation

```bash
# 1. Clone the repository (replace with your actual URL)
git clone https://github.com/yourname/fastcontainer.git
cd fastcontainer

# 2. Recommended: install with uv (fastest, matches the sample scripts)
uv sync --force-reinstall

# Alternative: install with pip (editable mode)
# pip install --force-reinstall -e .
```

### Design Philosophy — Built for R&D, not production hardening

**fastcontainer is an R&D tool first and foremost.**

It is deliberately optimized for **maximum flexibility, speed of iteration, and ease of experimentation** — especially for GPU-heavy machine learning, CUDA development, custom driver passthrough, and rapid prototyping of complex software stacks.

This means it makes the following intentional trade-offs:

- Runs as root and uses full systemd-nspawn with generous host bindings (devices, `/dev`, `/sys`, user directories, etc.).
- Prioritizes direct hardware access (NVIDIA GPUs, CUDA toolkit, etc.) over container isolation.
- Does **not** implement rootless mode, seccomp, AppArmor, or other production-grade sandboxing by default.
- Focuses on developer velocity: one YAML file, instant btrfs snapshot caching, profile inheritance for build variants, and free-form shell scripts in `RUN:` / `cmd:`.

**In short:**  
If you want to quickly spin up a reproducible environment with full GPU access, install whatever you want, bind your entire home directory for cache, and iterate in seconds — fastcontainer is perfect.

If you need a hardened, production-ready, multi-tenant container platform, this is **not** the tool for you (use Podman, Docker, or Kubernetes instead).

The goal is to feel closer to a super-powered `chroot + debootstrap + script` workflow than to a security-first container runtime.

### Quick Start

```bash
sudo fastcontainer build <containers_dir> <prepare.yaml> -p <profile> [-v] [--prune] [-s] [-D KEY=VALUE]... [-- <command...>]

sudo fastcontainer exec <containers_dir> <image-name> <command...> [-v]
```

### Interactive Shell (`-s` / `--shell`) — **New in v0.7.0**

The `-s` / `--shell` flag now gives you a smooth developer experience in **both** cases:

**On build failure** (any `RUN:` step fails):
- Automatically drops you into an interactive bash shell inside the **temporary failed layer**.
- You are in the exact filesystem state right after the failing command.
- Full nspawn environment (GPU devices, binds, tmpfs, etc.) is preserved.

**On successful build** (fresh build **or** cache hit):
- Instead of running the profile’s `cmd:` (or any trailing command), it drops you directly into an interactive login shell in the **final container**.
- Perfect when you want to explore, test, or poke around immediately after building.

```bash
# Build + get a shell (works on success or failure)
sudo fastcontainer build ... -p myprofile -s

# Traditional behavior (run cmd: or trailing command)
sudo fastcontainer build ... -p myprofile
```

**Details**
- When `--shell` is used, any trailing command after `--` is ignored.
- On failure you still see the clear failure banner first.
- Type `exit` (or Ctrl+D) to leave the shell. On failure the build still fails and temporary files are cleaned up.

This is one of the most powerful features for rapid iteration.

### Variables (`-D`)

fastcontainer supports variable substitution using the **`{{VAR}}`** syntax **everywhere** in the YAML file:

- `add:` (nspawn flags)
- `RUN:` / `USE:` steps
- `check:`
- `cmd:`
- `base.create:`

**Command line syntax:**
```bash
sudo fastcontainer build ... \
  -D HOST_CACHE=/home/noname/.cache \
  -D HF_CACHE=/home/noname/.cache/huggingface
```

**Example YAML:**

```yaml
add:
  - "--bind={{HOST_CACHE}}:/root/.cache"

steps:
  - RUN: |
      echo "Host cache is {{HOST_CACHE}}"
```

**Rules:**
- Only `{{VAR}}` syntax is supported.
- `{{ROOT}}` is a **reserved** special placeholder (automatically injected — do **not** define it with `-D`).
- Undefined variables cause a clear build error.
- Variables are expanded before fingerprint calculation, so caching stays reliable.

### Profiles & Inheritance (v0.6.0+)

Profiles are the heart of fastcontainer. They support full inheritance:

```yaml
profiles:
  common:
    add:
      - "--tmpfs=/var/tmp"
      - "--private-users=no"
    steps:
      - RUN: |
          apt-get update && apt-get install -y ...

  cuda:
    extend: common          # inherits flags + steps from "common"
    steps:
      - RUN: |
          # CUDA-specific steps
```

**Key rules:**

- `extend:` pulls in **all** flags and steps from the parent.
- `add:` appends new flags (child flags always come **after** parent flags).
- `remove:` (or `del:`) removes specific flags.
- `steps:` are additive (child steps run **after** parent steps).
- `cmd:` and `check:` are **not** inherited — they only apply to the profile where they are defined.
- Only the **final (leaf) profile** ever executes a `cmd:` (unless overridden by `--shell` or CLI command).

### Reusable Snippets (`snippets:` + `USE:`) — **New**

(unchanged — kept as-is)

### Base specification

(unchanged)

### `check:` (part of cache key)

(unchanged)

### Post-build command (`cmd:`)

You can define a default command that runs automatically after a successful build:

```yaml
cmd: |
  echo "=== Starting llama-bench ==="
  /llama.cpp/build/bin/llama-bench ...
```

You can also override it from the CLI:

```bash
sudo fastcontainer build ... -p run-llama-cpp -- /bin/bash -l
```

**Note:** If you use `-s` / `--shell`, the `cmd:` (and any trailing command) is **skipped** and you get an interactive shell instead.

### Logging / Output

(unchanged)

### Examples

```bash
# Basic build with variables
sudo fastcontainer build /disk/fastcontainer ./sample/sample.yaml -p default \
  -D HOST_CACHE=/home/noname/.cache

# Full GPU + runtime variant (now using snippets)
sudo fastcontainer build /disk/fastcontainer ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama-cpp

# Build and immediately drop into a shell (success or failure)
sudo fastcontainer build ... -p default -s

# Run a one-off command instead of the profile's cmd:
sudo fastcontainer build ... -p run-llama-cpp -- /bin/bash -l

# Verbose build
sudo fastcontainer build ... -v

# Prune intermediate layers after success
sudo fastcontainer build ... --prune
```

**Final image name format:**  
`<effective_base>-<profile>-<40hex_fingerprint>`

### Other features

- `-s` / `--shell`: interactive shell on failure **or** success (v0.7.0)
- `--prune`: delete all intermediate layers after a successful build.
- Automatic build lock (`.fastcontainer.lock`)
- All temporary subvolumes are cleaned up on success or failure.
- Every intermediate and final layer contains a `fastcontainer.json` manifest with full build history.

### Contributing & Development

```bash
# Regenerate the full source prompt for AI assistance
bash scripts/project-to-prompt.sh
```
