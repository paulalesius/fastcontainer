The project is essentially a tiny, lightweight “Dockerfile → image” tool that:

- Takes a very simple prepare.yaml (base image + list of RUN commands).
- Builds a final container subvolume using btrfs copy-on-write snapshots for layering.
- Provides fast per-step caching *during a single build run* (previous successful steps stay cached while you fix a failing one).
- Automatically prunes all intermediate layers on a successful build, keeping your containers directory clean.
- Writes a small fastcontainer.json manifest inside the final image for introspection.

NEW: base creation
------------------
`base` can now be either:

a) a simple string (100% backwards compatible):
   base: ubuntu-noble

b) an object that creates the base on-the-fly:
   base:
     name: ubuntu-noble
     create: |
       debootstrap --variant=minbase noble . http://archive.ubuntu.com/ubuntu/
       chroot . apt-get clean

   The create script is executed **on the host** with its working directory set to the new subvolume.
   This is exactly what debootstrap, pacstrap, etc. expect.

   IMPORTANT SAFETY NOTE:
   - Commands run on the **host**. Absolute paths starting with / affect the host filesystem.
   - For cleanup commands that should run inside the new rootfs, use `chroot . command` (as shown above).

It is intentionally minimal (only RUN steps for now) and designed to be extremely fast and simple to understand/maintain. No Docker daemon, no Buildah/Podman required — just root + btrfs + systemd-nspawn.
