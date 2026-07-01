# system-packages/ — offline OS-level (.deb) packages

Some dependencies aren't Python packages at all, or don't publish a prebuilt Linux
aarch64 wheel on PyPI — `pip`/wheelhouse can't help with those. This folder holds
their Debian `.deb` files instead, so they can be installed offline with `dpkg`.

## pyscard (the ACR1252U reader's PC/SC bindings)

`pyscard` (in `requirements.txt`) has **no prebuilt Linux aarch64 wheel on PyPI** —
only Windows and macOS. Building it from source needs a compiler, `swig`, and
`libpcsclite-dev`, none of which are worth adding to an offline Pi's toolchain just
for one package. Instead, install the real **Debian arm64 package**:

- Package: `python3-pyscard` (Debian bookworm, the base Raspberry Pi OS is built on)
- Download (on a machine with internet — any OS, no aarch64 needed, it's just a file):
  `https://deb.debian.org/debian/pool/main/p/pyscard/python3-pyscard_2.0.5-1+b2_arm64.deb`
  (if that exact filename 404s, browse `https://deb.debian.org/debian/pool/main/p/pyscard/`
  for the current arm64 build)
- Dependencies: only `libc6` and `python3` — both already on any Raspberry Pi OS install.
  Nothing else to fetch.
- Version note: `requirements.txt` pins `pyscard>=2.0.7`; Debian ships `2.0.5`. Checked
  the upstream changelog for 2.0.6/2.0.7 — both are Windows-build/packaging fixes only,
  nothing touching the Linux/PC-SC API. `2.0.5` is fine here.

Put the downloaded `.deb` in this folder, copy the whole `deploy/` tree onto the Pi
(SD card / USB stick) along with the rest of the project, then either:

- **Online** (temporary internet on the Pi): `install.sh` just runs
  `apt-get install -y python3-pyscard` like any other OS package — this folder isn't
  needed in that case.
- **Offline** (the real scenario here): `install.sh` looks for a `.deb` in this folder
  and runs `dpkg -i` on it automatically.

## Why the venv can still see it

`install.sh` creates the venv with `--system-site-packages`, so a package installed at
the **system** Python level (which is where `dpkg -i` puts it) is visible inside the venv
too — no need to get `pyscard` into the wheelhouse or the venv directly.

The `.deb` files themselves are **not committed** (see `.gitignore`) — like the
wheelhouse, they're architecture-specific binary artifacts, fetched per image.
