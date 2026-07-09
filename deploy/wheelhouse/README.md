# wheelhouse/ — offline Python packages

This folder holds pre-downloaded Python wheels so the Raspberry Pi can build its
virtualenv **with no internet**.

- Populate it by running `deploy/install/build-wheelhouse.sh` on a machine **with
  internet**. That script targets **Python 3.13 / cp313** (Raspberry Pi OS trixie)
  and also auto-downloads the `python3-pyscard` `.deb` into `../system-packages/`.
  Cross-download from Windows/x86_64 works via pip's `--platform` flags — no aarch64
  host required.
- `deploy/install/install.sh` installs from here with
  `pip --no-index --find-links` when wheels are present. It also preflights the
  wheelhouse ABI so a stale cp311 kit fails loud on a cp313 Pi instead of
  partially installing.
- On the Pi run: `sudo bash deploy/install/install.sh` (use `bash` so a missing
  `+x` bit from an exFAT copy does not matter).

The `.whl` / `.tar.gz` files are **not committed** (see `.gitignore`) — they are large,
architecture-specific build artifacts. Generate them per target image.
