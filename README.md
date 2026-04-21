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

### Interactive Debug Shell on Failure (`-s` / `--shell-on-fail`) — **New in v0.6.0**

When a `RUN:` step fails during the build, fastcontainer can **automatically drop you into an interactive bash shell** inside the exact temporary layer where the failure occurred.

```bash
sudo fastcontainer build ... -p myprofile -s
# or long form
sudo fastcontainer build ... --shell-on-fail
```

**What you get:**
- The failure banner with full command + output (as before)
- A full login shell (`bash -l`) with **exactly the same nspawn flags** as your profile (GPU devices, binds, tmpfs, environment, etc.)
- The filesystem is in the precise state right after the failing command
- You can inspect files, run commands, install packages, edit scripts, test fixes, etc.

Type `exit` (or Ctrl+D) when you are done. The build will still fail afterwards and all temporary files will be cleaned up.

This is one of the most powerful features for rapid iteration on complex builds.

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
- `{{ROOT}}` is a **reserved** special placeholder. It is automatically replaced by the actual container path at runtime. Do **not** define it with `-D ROOT=...`.
- Any other `{{VAR}}` that is not passed via `-D` will cause an immediate clear build error.
- Variables are expanded before fingerprint calculation, so caching remains reliable and reproducible.

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
- Only the **final (leaf) profile** ever executes a `cmd:` (or the optional trailing CLI command).

### Reusable Snippets (`snippets:` + `USE:`) — **New**

Tired of repeating the same long `RUN:` blocks (CUDA install, `uv` setup, `git clone`, build commands, etc.) across profiles?  
fastcontainer now lets you define **reusable command snippets** at the top level.

```yaml
# Top of your YAML file
base:
  name: ubuntu24.04-cu132-llama-cpp
  create: |
    debootstrap ...

snippets:
  apt-update-and-upgrade:
    RUN: |
      apt-get update
      apt-get -y upgrade

  install-build-deps:
    RUN: |
      apt install -y software-properties-common wget git ncurses-term libssl-dev cmake

  install-cuda:
    RUN: |
      wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
      dpkg -i cuda-keyring_1.1-1_all.deb
      apt-get update
      apt-get -y install cuda-toolkit

  clone-llama-cpp: |
    git clone https://github.com/ggml-org/llama.cpp.git

  build-llama-cpp:
    RUN: |
      . /etc/profile
      export CUDA_HOME=/usr/local/cuda
      cd llama.cpp
      cmake -B build -DGGML_CUDA=ON && cmake --build build --config Release -j $(nproc)
```

Then in any profile simply **reuse** them:

```yaml
profiles:
  common:
    steps:
      - USE: apt-update-and-upgrade
      - USE: install-build-deps

  cuda:
    extend: common
    steps:
      - USE: install-cuda

  llama-cpp:
    extend: cuda
    steps:
      - USE: clone-llama-cpp
      - USE: build-llama-cpp
```

**Why this is great**
- Dramatically cleaner YAML files (especially with CUDA / llama.cpp / Rust / uv setups).
- Snippets are expanded exactly like normal `RUN:` steps.
- Full support for inheritance (`extend:`), variables (`{{VAR}}`), and layer caching/fingerprinting.
- You can still mix `RUN:` and `USE:` freely in the same profile.
- Clear error message if you typo a snippet name.

### Base specification

```yaml
base:
  name: ubuntu24.04-cu132
  create: |
    debootstrap --variant=minbase noble . http://archive.ubuntu.com/ubuntu/
  add:                          # optional default flags for every profile
    - "--bind={{HOST_CACHE}}:/var/cache/apt"
```

### `check:` (part of cache key)

If the final image already exists, the `check:` script is executed inside it.  
If it fails, the image is deleted and a full rebuild is forced.

The check script is included in the image fingerprint, so changing it always produces a new final image.

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

### Logging / Output

- **Default** (no `-v`): clean, plain ASCII progress only.
- `-v` / `--verbose`: full live output of every build step + internal commands.
- **On failure**: the complete output of the failing step is **always** printed (even without `-v`).

### Examples

```bash
# Basic build with variables
sudo fastcontainer build /disk/fastcontainer ./sample/sample.yaml -p default \
  -D HOST_CACHE=/home/noname/.cache

# Full GPU + runtime variant (now using snippets)
sudo fastcontainer build /disk/fastcontainer ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama-cpp

# Run a one-off command instead of the profile's cmd:
sudo fastcontainer build ... -p run-llama-cpp -- /bin/bash -l

# Verbose build
sudo fastcontainer build ... -v

# Build with debug shell on failure
sudo fastcontainer build ... -p default -s
```

**Final image name format:**  
`<effective_base>-<profile>-<40hex_fingerprint>`

### Other features

- `-s` / `--shell-on-fail`: drop into an interactive debug shell when a build step fails (v0.6.0)
- `--prune`: delete all intermediate layers after a successful build.
- Automatic build lock (`.fastcontainer.lock`) — prevents two builds from running at the same time in the same directory.
- All temporary subvolumes are cleaned up on success or failure.
- Every intermediate and final layer contains a `fastcontainer.json` manifest with full build history.

### Contributing & Development

```bash
# Regenerate the full source prompt for AI assistance
bash scripts/project-to-prompt.sh
```
