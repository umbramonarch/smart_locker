# system-packages/ — offline OS-level (.deb) packages

Some dependencies aren't Python packages at all, or don't publish a prebuilt Linux
aarch64 wheel on PyPI — `pip`/wheelhouse can't help with those. This folder holds
their Debian `.deb` files instead, so they can be installed offline with `dpkg`.

Running `deploy/install/build-wheelhouse.sh` **automatically downloads** the
correct `python3-pyscard_*_arm64.deb` into this folder — so in the normal case you
do not need to touch it. The notes below apply only if you stage it by hand.

## pyscard (the ACR1252U reader's PC/SC bindings)

`pyscard` (in `requirements.txt`) has **no prebuilt Linux aarch64 wheel on PyPI** —
only Windows and macOS. Building it from source needs a compiler, `swig`, and
`libpcsclite-dev`, none of which are worth adding to an offline Pi's toolchain just
for one package. Instead, install the real **Debian arm64 package**:

- Package: `python3-pyscard` — Debian **trixie**, the base Raspberry Pi OS is built on
  as of the 6.18 kernel line. (Earlier Raspberry Pi OS used bookworm + Python 3.11;
  the same machine now ships trixie + Python 3.13 by default. A bookworm `.deb`
  will NOT import under Python 3.13 — ABI mismatch.)
- Current version on trixie: **`2.2.2-1`** (satisfies `requirements.txt`'s `pyscard>=2.0.7` pin).
- Download (on a machine with internet — any OS, no aarch64 needed, it's just a file):
  `https://deb.debian.org/debian/pool/main/p/pyscard/python3-pyscard_2.2.2-1_arm64.deb`
  (if that exact filename 404s, browse `https://deb.debian.org/debian/pool/main/p/pyscard/`
  for the current arm64 build — pick the one whose version satisfies `>=2.0.7`).
- Dependencies: only `libc6` and `python3` — both already on any Raspberry Pi OS
  install. Nothing else to fetch.

Put the downloaded `.deb` in this folder (alongside the wheelhouse, which `build-wheelhouse.sh`
handles automatically), copy the whole `deploy/` tree onto the Pi (SD card / USB stick)
along with the rest of the project, then either:

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
