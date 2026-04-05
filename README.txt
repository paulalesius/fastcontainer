Temporary subvolumes & pruning policy
-------------------------------------
fastcontainer uses strict naming conventions so it can never accidentally delete your final images or bases:

- Final images:          `<effective_base>-<40hex_yaml>`
- Base subvolumes:       `<name>` or `<name>-<16hex>` (when using `create:`)
- Intermediate layers:   `__<effective_base>-<40hex>`   ← automatically pruned on success
- Temporary volumes:     `_...-<32hex_uuid>` (start with single underscore) ← always cleaned up

This design gives you:
- Fast per-step caching **during a single build run** (great when iterating on a failing step)
- A clean containers directory after every successful build (no disk bloat)
- Zero risk of deleting the wrong subvolumes
- Old bases are intentionally kept when the `create:` script changes (different hash suffix)

Intermediate layers are **not** kept across different `.yaml` files — this is intentional to avoid filling your disk with old layers.

Profiles
--------
Every build now requires a `profiles:` section in the YAML. Each profile defines the exact
`systemd-nspawn` command line (including binary name and all flags). The `systemd-nspawn`
binary itself is resolved automatically from your environment/PATH.

You must specify which profile to use with the required `-p/--profile` CLI flag.

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
