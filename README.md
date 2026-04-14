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
sudo fastcontainer build <containers_dir> <prepare.yaml> -p <profile> [-v] [--prune] [-D KEY=VALUE]... [-- <command...>]

sudo fastcontainer exec <containers_dir> <image-name> [--] <command...> [-v]
```

### Variables in nspawn flags (`-D`)

fastcontainer supports shell-style variable expansion inside **any string** in `add:` lists (including the new `base.add:`).

**Command line syntax:**
```bash
sudo fastcontainer build ... -D KEY1=value1 -D KEY2=value2 ...
```

**In your YAML:**
```yaml
add:
  - "-D"
  - "{{ROOT}}"
  - "--bind=${HOST_CACHE}:/root/.cache"
  - "--bind=${HF_CACHE}:/root/.cache/huggingface"
```

- Supports both `$VAR` and `${VAR}` syntax.
- `ROOT` is a **reserved** name — attempting `-D ROOT=...` will raise a clear error.
- Variables are expanded **before** fingerprint calculation and root validation, so caching stays perfectly reproducible.
- Undefined variables cause an immediate build error with a helpful message.

This is especially useful for host-specific paths (caches, home directories, driver versions, Hugging Face cache, etc.).

### Logging / Output
- **Default** (no `-v`): clean, plain ASCII progress only — shows current profile + each step as it is built.
- `-v` / `--verbose`: full live output of every build step + internal commands.
- **On failure**: the complete output of the failing step is **always** printed (even without `-v`) before exiting.

### Examples
```bash
# Build default variant with a variable
sudo fastcontainer build /disk/fastcontainer ./sample/sample.yaml -p default \
  -D HOST_CACHE=/home/noname/.cache

# Build a specialized variant (inherits common steps + adds its own)
sudo fastcontainer build /disk/fastcontainer ./sample/ubuntu24.04-cu132-llama-cpp.yaml -p run-llama-cpp \
  -D HF_CACHE=/home/noname/.cache/huggingface

# Run post-build command from profile (or override it)
sudo fastcontainer build ... -p run-llama-cpp -- /bin/bash -l

# Verbose build (shows full step output)
sudo fastcontainer build /disk/fastcontainer ./sample/sample.yaml -p default -v
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

3. Dictionary with default nspawn flags (`add:` — new in v0.4.0+):
   ```yaml
   base:
     name: ubuntu24.04-cu132
     create: |
       debootstrap noble . http://archive.ubuntu.com/ubuntu/
     add:
       - --bind=${HOST_CACHE}:/var/cache/apt
       - --bind=${HOST_CACHE}:/var/lib/apt/lists
   ```

**Best practice**: Use `base.add:` (or variables in root profiles) for any flags you want **every** profile to inherit (especially apt caches, ccache, cargo, uv, pip, downloads, NVIDIA driver binds, etc.).

(The rest of the README — Temporary subvolumes, Profiles tree, Post-build command, `check:`, etc. — remains unchanged and accurate.)

---

**Note on evolving specification**
The YAML format is stable for the new profile tree. Container deletion policy and advanced cleanup commands are still planned for future releases.

Contributing & Development
--------------------------
Run `./scripts/project-to-prompt.sh` to generate a ready-to-paste prompt with all source files.
