fastcontainer - Minimal btrfs + systemd-nspawn layered container builder
============================================================================

<div align="center">
  <img src="logo.jpg" alt="fastcontainer">
</div>

Usage
-----

Build a container:
    sudo fastcontainer build <containers_dir> <prepare.yaml> -p <profile> [-q]

Run a command inside a built container:
    sudo fastcontainer exec <containers_dir> <image-name> [--] <command...> [-q]

Interactive shell example:
    sudo fastcontainer exec <containers_dir> <image-name> -- bash -l

Examples:
    sudo fastcontainer build /disk/containers ./sample/sample.yaml -p default

    # Run a one-off command
    sudo fastcontainer exec /disk/containers ubuntu-custom-default-1a2b3c... -- apt-get install -y htop

    # Open an interactive shell
    sudo fastcontainer exec /disk/containers ubuntu-custom-default-1a2b3c... -- bash -l

The image name is the final subvolume name printed at the end of a successful build
(e.g. `ubuntu-custom-default-abc123def456...`).

Base specification
------------------
The `base:` key in the YAML supports two formats:

1. Simple string — when the base btrfs subvolume already exists in the containers directory:

        base: ubuntu-noble

2. Dictionary with creation script — fastcontainer will automatically create the base the first time:

        base:
          name: ubuntu-custom
          create: |
            debootstrap --variant=minbase noble . http://archive.ubuntu.com/ubuntu/
            chroot . echo "test command inside chroot"

When using the dictionary form with `create:`, the base subvolume on disk is named `<name>-<16hex>` (a short hash of the script). Changing the creation script automatically produces a new base.

Temporary subvolumes & pruning policy
-------------------------------------
fastcontainer uses strict naming conventions so it can never accidentally delete your final images or bases:

- Final images:          `<effective_base>-<profile>-<40hex_yaml>`
- Base subvolumes:       `<name>` or `<name>-<16hex>` (when using `create:`)
- Intermediate layers:   `__<effective_base>-<40hex>`   ← automatically pruned on success
- Temporary volumes:     `_...-<32hex_uuid>` (start with single underscore) ← always cleaned up

This design gives you:
- Fast per-step caching **during a single build run** (great when iterating on a failing step)
- A clean containers directory after every successful build (no disk bloat)
- Zero risk of deleting the wrong subvolumes
- Old bases are intentionally kept when the `create:` script changes (different hash suffix)

The layer hash chain and final image name are **profile-aware**. Different profiles produce completely different hashes and final subvolume names.

Profiles
--------
Every build requires a `profiles:` section in the YAML. Each profile defines the exact
`systemd-nspawn` command line (including the binary name and all flags).

You must specify which profile to use with the required `-p/--profile` flag.

Example:
```yaml
profiles:
  default:
    nspawn:
      - "systemd-nspawn"
      - "-D"
      - "{{ROOT}}"
      - "--tmpfs=/var/tmp"
      - "--private-users=no"
      - "--resolv-conf=replace-stub"
      - "--timezone=off"
  host-network:
    nspawn:
      - "systemd-nspawn"
      - "-D"
      - "{{ROOT}}"
      - "--network-host"
      - "--tmpfs=/var/tmp"
      - "--private-users=no"
      - "--resolv-conf=replace-stub"
      - "--timezone=off"

Contributing & Development
--------------------------
Developers wanting to take part in the project can create feature requests or issues through GitHub.

To ask the project questions in an LLM prompt, simply run:
./scr/project-to-prompt.sh
This script collects all source files into a clean, ready-to-paste format.
