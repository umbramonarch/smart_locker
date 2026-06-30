# wheelhouse/ — offline Python packages

This folder holds pre-downloaded Python wheels so the Raspberry Pi can build its
virtualenv **with no internet**.

- Populate it by running `deploy/install/build-wheelhouse.sh` on a machine **with
  internet** that matches the Pi's architecture (aarch64 / 64-bit Raspberry Pi OS)
  and Python version. The simplest option is to run it on the Pi itself while it
  still has a temporary internet connection, before moving it into the company network.
- `deploy/install/install.sh` automatically installs from here (`pip --no-index
  --find-links`) when wheels are present; otherwise it falls back to PyPI (online only).

The `.whl` / `.tar.gz` files are **not committed** (see `.gitignore`) — they are large,
architecture-specific build artifacts. Generate them per target image.
