fastcontainer - Minimal btrfs + systemd-nspawn layered container builder
============================================================================

<div align="center">
  <img src="logo.jpg" alt="fastcontainer">
</div>

### Design Philosophy — Built for R&D, not production hardening

**fastcontainer is an R&D tool first and foremost.**

It is deliberately optimized for **maximum flexibility, speed of iteration, and ease of experimentation** — especially for GPU-heavy machine learning, CUDA development, custom driver passthrough, and rapid prototyping of complex software stacks.

This means it makes the following intentional trade-offs:

- Runs as root and uses full systemd-nspawn with generous host bindings (devices, /dev, /sys, user directories, etc.).
- Prioritizes direct hardware access (NVIDIA GPUs, CUDA toolkit, etc.) over container isolation.
- Does **not** implement rootless mode, seccomp, AppArmor, or other production-grade sandboxing by default.
- Focuses on developer velocity: one YAML file, instant btrfs snapshot caching, profile inheritance for build variants, and free-form shell scripts in `RUN:` / `cmd:`.

**In short:**
If you want to quickly spin up a reproducible environment with full GPU access, install whatever you want, bind your entire home directory for cache, and iterate in seconds — fastcontainer is perfect.

If you need a hardened, production-ready, multi-tenant container platform, this is **not** the tool for you (use Podman, Docker, or Kubernetes instead).

The goal is to feel closer to a super-powered `chroot + debootstrap + script` workflow than to a security-first container runtime.

### Usage
```bash
sudo fastcontainer build <containers_dir> <prepare.yaml> -p <profile> [-v] [--prune] [-- <command...>]

sudo fastcontainer exec <containers_dir> <image-name> [--] <command...> [-v]
```

**Logging / Output**
- **Default** (no `-v`): clean, plain ASCII progress only — shows current profile + each step as it is built.
- `-v` / `--verbose`: full live output of every build step + internal commands.
- **On failure**: the complete output of the failing step is **always** printed (even without `-v`) before exiting.

### Examples
```bash
# Build default variant
sudo fastcontainer build /disk/containers ./sample/sample.yaml -p default

# Build a specialized variant (inherits common steps + adds its own)
sudo fastcontainer build /disk/containers ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama

# Run post-build command from profile (or override it)
sudo fastcontainer build /disk/containers ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama -- /bin/bash -l

# Verbose build (shows full step output)
sudo fastcontainer build /disk/containers ./sample/sample.yaml -p default -v
```

The final image name is always profile-aware: `<effective_base>-<profile>-<40hex_fingerprint>`.

### Base specification

The `base:` key supports three formats:

1. Simple string (assumes a pre-existing subvolume):
   ```yaml
   base: ubuntu-noble
   ```

2. Dictionary with creation script (automatically cached by content hash):
   ```yaml
   base:
     name: ubuntu-custom
     create: |
       debootstrap --variant=minbase noble . http://archive.ubuntu.com/ubuntu/
   ```

3. Dictionary with default nspawn flags (`add:` — new in v0.3.0):
   ```yaml
   base:
     name: ubuntu24.04-cu132
     create: |
       debootstrap noble . http://archive.ubuntu.com/ubuntu/
     add:
       - --bind=/home/noname/fastcontainers/cache/apt-cache:/var/cache/apt
       - --bind=/home/noname/fastcontainers/cache/apt-lists:/var/lib/apt/lists
       # ... put all your common caching binds, NVIDIA driver binds, etc. here
   ```

**Best practice**: Use `base.add:` for any flags you want **every** profile to inherit (especially apt caches, ccache, cargo, uv, pip, downloads, NVIDIA driver binds, etc.).  
These flags are automatically injected into every **root profile** (any profile without `extend:`) and then inherited normally by child profiles.

### Temporary subvolumes & pruning policy

fastcontainer uses btrfs subvolumes for fast, copy-on-write layered builds and precise caching:

- **Base image**: Stored under its `effective_name` (either the plain name or `name-<short-hash>` when a `create:` script is used).
- **Intermediate cached layers**: Named `__<effective_base>-<40-character-sha1-hash>`.  
  These are created for every `RUN` step and are **kept by default**.  
  The hash is computed from the previous layer + the exact command + the profile’s full resolved nspawn flags, so caching works perfectly across the entire profile tree.
- **Final profile images**: Named `<effective_base>-<profile>-<40-hex-fingerprint>`.
- **Temporary working subvolumes**: Names starting with `_` (e.g. `_…-temp-…`, `_…-final-…`). These are automatically cleaned up after each step.

**With hierarchical profiles (`extend:`)**:  
Child profiles start from their parent’s *final image* (not from the base). Only the child’s own delta steps generate new intermediate layers. This maximises cache reuse across variants while keeping the build semantics users expect.

**Pruning (`--prune` flag)**:  
After a successful build, `--prune` deletes **all** intermediate layers matching `__<effective_base>-*` for that base.  
Final images (including parents in the inheritance chain) are **never** deleted, so child profiles and future builds can reuse them instantly.

### Profiles – tree of build variants (hierarchical execution)

Profiles now form a proper inheritance tree with **hierarchical build execution**:

- A root profile (no `extend`) is built from the base image using **only its own** nspawn flags and its `steps`.
- When you build a child profile, fastcontainer first ensures the entire parent chain is built.
- It then performs a **delta build**: only the child’s own additional `steps` are executed, using the child’s full merged nspawn flags (`add`/`remove` + inherited flags).
- This lets child profiles safely use flags that only make sense *after* parent steps have run (e.g. `--user=agent`, `--chdir=/home/agent`, extra GPU binds, custom environment variables, etc.).

The result is clean, predictable multi-stage builds that feel like proper inheritance while keeping perfect btrfs snapshot caching.

#### YAML structure
```yaml
base: ...

profiles:
  common:                          # root profile
    add:
      - "systemd-nspawn"
      - "-D"
      - "{{ROOT}}"
      - "--tmpfs=/var/tmp"
      # ... other common flags
    steps:
      - RUN: |
          apt-get update
          apt-get install -y curl git
          # user creation, base packages, etc. can go here

  cuda:                            # first child – inherits from common
    extend: common
    add: [...]                     # extra GPU flags
    steps:
      - RUN: |
          # CUDA-specific steps (run with cuda flags)

  llama-cpp:                       # deeper child
    extend: cuda
    steps:
      - RUN: |
          git clone https://github.com/ggml-org/llama.cpp.git
          # build steps (run with llama-cpp flags)

  run-llama-cpp:                   # runtime variant
    extend: llama-cpp
    add:
      - --user=agent
      - --chdir=/home/agent
      # ... more runtime binds
    cmd: |
      echo "=== Starting llama-bench ==="
      /llama.cpp/build/bin/llama-bench ...

  minimal:                         # alternative branch
    extend: common
    steps: []                      # no extra steps
```

#### Key rules
- Root profiles (no `extend`) must provide a full `add:` list of flags.
- Child profiles inherit **all** steps from their parent and append their own.
- Step order is always preserved (parent steps first).
- The selected profile (`-p`) determines the exact step list used for the build.
- Top-level `steps:` (old flat list) has been removed. Migrate by moving your steps under the root profile you usually use.

### Post-build command (`cmd:`)
Supports both list (argv) and free-form shell script (`|` block) – exactly like `RUN:`.

### Build with pruning
```bash
sudo fastcontainer build ... -p llama-cpp --prune
```

---

**Note on evolving specification**  
The YAML format is stable for the new profile tree. Container deletion policy and advanced cleanup commands are still planned for future releases.

Contributing & Development
--------------------------
Run `./scripts/project-to-prompt.sh` to generate a ready-to-paste prompt with all source files.
