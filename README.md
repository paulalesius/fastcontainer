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
sudo fastcontainer build <containers_dir> <prepare.yaml> -p <profile> [-q] [--prune] [-- <command...>]

sudo fastcontainer exec <containers_dir> <image-name> [--] <command...> [-q]
```

### Examples
```bash
# Build default variant
sudo fastcontainer build /disk/containers ./sample/sample.yaml -p default

# Build a specialized variant (inherits common steps + adds its own)
sudo fastcontainer build /disk/containers ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama

# Run post-build command from profile (or override it)
sudo fastcontainer build /disk/containers ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama -- /bin/bash -l
```

The final image name is always profile-aware: `<effective_base>-<profile>-<40hex_yaml>`.

### Base specification
The `base:` key supports two formats (unchanged):

1. Simple string:
   ```yaml
   base: ubuntu-noble
   ```

2. Dictionary with creation script (cached by hash):
   ```yaml
   base:
     name: ubuntu-custom
     create: |
       debootstrap --variant=minbase noble . http://archive.ubuntu.com/ubuntu/
   ```

### Temporary subvolumes & pruning policy
(unchanged – same safety guarantees, intermediate layers `__*`, final images, etc.)

### Profiles – now a tree of build variants
**This is the biggest change in v0.2.0.**

- Profiles define **both** nspawn flags (`add`/`remove`) **and** build steps (`steps`).
- `extend:` now inherits **flags + steps**, letting you build a natural tree of variants from a common base.
- All variants share the same base image but can have different intermediate layers.
- Layer caching works perfectly across branches (shared prefix steps reuse the exact same btrfs subvolumes).
- `cmd:` (post-build command) is still supported and can be a free-form shell script or argv list.

#### YAML structure
```yaml
base: ...

profiles:
  common:                          # root profile (defines base steps + flags)
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

  cuda:                            # first branch – inherits common steps
    extend: common
    add: [...]                     # extra GPU flags
    steps:
      - RUN: |
          # CUDA-specific steps...

  llama-cpp:                       # deeper branch – inherits common + cuda
    extend: cuda
    steps:
      - RUN: |
          git clone https://github.com/ggml-org/llama.cpp.git
          # build steps...

  minimal:                         # alternative short variant
    extend: common
    steps: []                      # no extra steps
```

#### Key rules
- Root profiles (no `extend`) must provide a full `add:` list of flags.
- Child profiles inherit **all** steps from their parent and append their own.
- Step order is always preserved (parent steps first).
- The selected profile (`-p`) determines the exact step list used for the build.
- Top-level `steps:` (old flat list) has been removed. Migrate by moving your steps under the root profile you usually use.

### Migration from older YAMLs
Move the old top-level `steps:` into your main/root profile:
```yaml
# Before (old)
steps:
  - RUN: ...

# After (new)
profiles:
  default:
    add: [...]
    steps:
      - RUN: ...
```

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
