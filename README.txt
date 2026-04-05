The project is essentially a tiny, lightweight “Dockerfile → image” tool that:

- Takes a very simple prepare.yaml (base image + list of RUN commands).
- Builds a final container subvolume using btrfs copy-on-write snapshots for layering.
- Provides fast per-step caching *during a single build run* (previous successful steps stay cached while you fix a failing one).
- Automatically prunes all intermediate layers on a successful build, keeping your containers directory clean.
- Writes a small fastcontainer.json manifest inside the final image for introspection.

It is intentionally minimal (only RUN steps for now) and designed to be extremely fast and simple to understand/maintain. No Docker daemon, no Buildah/Podman required — just root + btrfs + systemd-nspawn.
