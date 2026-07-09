# system-packages/ — offline OS-level (.deb) packages

The production Pi is **never-networked and does not use apt**. Anything not already
on the OS image is installed offline with `dpkg -i` from this folder.

`deploy/install/build-wheelhouse.sh` **auto-downloads** the full set below (pinned
to the versions a real trixie Pi pulled during an online `install.sh` validation).
Missing any required `.deb` fails the kit build — not a soft warning.

## What gets staged (from a real Pi online apt run)

| Package | Role | On Raspberry Pi OS Full by default? |
|---|---|---|
| `libccid` | USB CCID driver for ACR1252U | **No** — staged |
| `pcscd` | PC/SC daemon | **No** — staged |
| `libintl-perl` | dep of pcsc-tools | **No** — staged |
| `libpcsc-perl` | dep of pcsc-tools | **No** — staged |
| `pcsc-tools` | `pcsc_scan` diagnostics | **No** — staged |
| `python3-pyscard` | Python PC/SC bindings | **No** — staged |
| `unclutter` | hide mouse cursor in kiosk | **No** — staged |
| `cifs-utils`, `curl`, `python3-venv`, `python3-pip`, `chromium*` | base kiosk | **Yes** on Full — not staged (huge) |

Pinned filenames (trixie / arm64, as of the validation install):

```
libccid_1.6.2-1_arm64.deb
pcscd_2.3.3-1_arm64.deb
libintl-perl_1.35-1_all.deb
libpcsc-perl_1.4.16-1+b3_arm64.deb
pcsc-tools_1.7.3-1_arm64.deb
python3-pyscard_2.2.2-1_arm64.deb
unclutter_8-25+nmu1_arm64.deb
```

## pyscard note

`pyscard` has **no** prebuilt Linux aarch64 wheel on PyPI. The Debian package is the
only offline path. A bookworm `.deb` will **not** import under Python 3.13.

## What install.sh does offline

1. `dpkg -i deploy/system-packages/*.deb` (all staged OS packages).
2. Verifies `pcscd`, `chromium`, `python3`, `mount.cifs`, and `import smartcard`.
3. Creates the venv with `--system-site-packages` so system-level pyscard is visible.
4. Installs **all other** Python deps from `../wheelhouse/` only
   (`pip --no-index --ignore-installed` — does **not** trust distro cryptography to
   paper over a incomplete wheelhouse; that is what made SQLAlchemy fail mid-install
   while crypto looked “already satisfied”).

## On the Pi

```bash
sudo bash deploy/install/install.sh
```

Always `sudo bash …` — never `sudo deploy/install/install.sh` (exFAT strips `+x` →
`command not found`).

The `.deb` files are **not committed** (see `.gitignore`).
