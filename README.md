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
sudo fastcontainer build <containers_dir> <prepare.yaml> -p <profile> [-v] [--prune] [-D KEY=VALUE]... [-- <command...>]

sudo fastcontainer exec <containers_dir> <image-name> <command...> [-v]
```

### Variables in nspawn flags (`-D`)

fastcontainer supports shell-style variable expansion **inside `add:` lists** (including `base.add:`).

**Command line syntax:**
```bash
sudo fastcontainer build ... -D HOST_CACHE=/home/noname/.cache -D HF_CACHE=/home/noname/.cache/huggingface
```

**In your YAML:**
```yaml
add:
  - "--bind=${HOST_CACHE}:/root/.cache"
  - "--bind=${HF_CACHE}:/root/.cache/huggingface"
```

- Supports both `$VAR` and `${VAR}` syntax.
- `ROOT` is a **reserved** name — attempting `-D ROOT=...` will raise a clear error.
- Variables are expanded **before** fingerprint calculation and root validation, so caching stays perfectly reproducible.
- Undefined variables cause an immediate build error with a helpful message.

### Profiles & Inheritance (v0.5.0)

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

  run-llama-cpp:
    extend: cuda
    add:
      - "--bind=/dev/nvidia0"
      # ... more GPU binds
    cmd: |
      echo "=== Starting benchmark ==="
      /llama.cpp/build/bin/llama-bench ...
    check: |
      # optional check that forces rebuild if it fails
      test -f /llama.cpp/build/bin/llama-bench
```

**Key rules (v0.5.0):**

- `extend:` pulls in **all** flags and steps from the parent.
- `add:` appends new flags (child flags always come **after** parent flags).
- `remove:` (or `del:`) removes specific flags.
- `steps:` are additive (child steps run **after** parent steps).
- `cmd:` and `check:` are **not** inherited — they only apply to the profile where they are defined.
- Only the **final (leaf) profile** ever executes a `cmd:` (or the optional trailing CLI command). Parent profiles never run `cmd:`.

### Base specification

```yaml
base:
  name: ubuntu24.04-cu132
  create: |
    debootstrap --variant=minbase noble . http://archive.ubuntu.com/ubuntu/
  add:                          # optional default flags for every profile
    - "--bind=${HOST_CACHE}:/var/cache/apt"
```

### `check:` (now part of cache key)

```yaml
check: |
  dpkg -l nginx 2>/dev/null | grep -q '^ii' || exit 1
  nginx -v 2>&1 | grep -q '1.2[4-9]'
```

- If the image exists, the check is executed inside it.
- If the check fails, the image is deleted and a full rebuild is forced.
- **Important (v0.5.0):** The check script is now included in the image fingerprint. Changing the check always produces a new final image name.

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

Only the final profile’s `cmd:` (or the CLI override) is executed.

### Logging / Output

- **Default** (no `-v`): clean, plain ASCII progress only.
- `-v` / `--verbose`: full live output of every build step + internal commands.
- **On failure**: the complete output of the failing step is **always** printed (even without `-v`).

### Examples

```bash
# Basic build with variable
sudo fastcontainer build /disk/fastcontainer ./sample/sample.yaml -p default \
  -D HOST_CACHE=/home/noname/.cache

# Full GPU + runtime variant
sudo fastcontainer build /disk/fastcontainer ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama-cpp

# Run a one-off command instead of the profile's cmd:
sudo fastcontainer build ... -p run-llama-cpp -- /bin/bash -l

# Verbose build
sudo fastcontainer build ... -v
```

Final image name format:  
`<effective_base>-<profile>-<40hex_fingerprint>`

### Other features

- `--prune`: delete all intermediate layers after a successful build.
- Automatic build lock (`.fastcontainer.lock`) — prevents two builds from running at the same time in the same directory.
- All temporary subvolumes are cleaned up on success or failure.
- Every intermediate and final layer contains a `fastcontainer.json` manifest with full build history.

### Contributing & Development

```bash
# Regenerate the full source prompt for AI assistance
bash scripts/project-to-prompt.sh
```

Run tests with:
```bash
uv sync
# (tests coming in future releases)
```
