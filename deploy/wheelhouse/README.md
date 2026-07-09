# wheelhouse/ — offline Python packages

This folder holds pre-downloaded Python wheels so the Raspberry Pi can build its
virtualenv **with no internet and no apt**.

## What belongs here

Everything in `requirements.txt` **except `pyscard`**, including **all transitive
dependencies** (e.g. `uvicorn[standard]` pulls `httptools`, `uvloop`, `watchfiles`,
`websockets`, `PyYAML`; `cryptography` pulls `cffi` / `pycparser`; FastAPI pulls
`starlette` / `pydantic` / …).

`pyscard` has **no** Linux aarch64 wheel on PyPI — it is staged as a Debian `.deb`
in `../system-packages/` (see that folder's README).

## How to populate

```bash
# On ANY machine with internet (Windows/x86 is fine):
deploy/install/build-wheelhouse.sh
```

That script:

1. Targets **Python 3.13 / cp313** (Raspberry Pi OS trixie) via pip's
   `--platform manylinux2014_aarch64` cross-download (or native aarch64 + version assert).
2. **Wipes** old `*.whl` / `*.tar.gz` so mixed-ABI debris cannot accumulate.
3. On a **Windows/macOS** build host, also force-downloads **Linux-only** wheels that
   environment markers would skip on the host (notably `uvloop` for
   `uvicorn[standard]` on the Pi).
4. Runs **`pip install --dry-run --ignore-installed --no-index --find-links …`** against
   the folder it just built — refuses to ship if any requirement (or transitive dep)
   is missing or wrong-ABI.
5. Auto-downloads the pinned **trixie** `python3-pyscard_*_arm64.deb` into
   `../system-packages/` (FATAL if that fails — production has no apt fallback).

## How the Pi uses it

`deploy/install/install.sh` (and in-field `update.sh`) install with:

```text
pip install --ignore-installed --no-index --find-links deploy/wheelhouse -r <reqs without pyscard>
```

`--ignore-installed` matters: the venv uses `--system-site-packages` so pyscard is
visible, but **system packages must not mask a incomplete wheelhouse** (a historic
failure mode: distro `cryptography` satisfied the pin while SQLAlchemy was still
missing a cp313 wheel).

On the Pi: `sudo bash deploy/install/install.sh` (`bash` so a missing `+x` from
exFAT does not matter).

The `.whl` / `.tar.gz` files are **not committed** (see `.gitignore`) — generate them
per target image on a machine with internet, then copy `deploy/` to the Pi on USB.
