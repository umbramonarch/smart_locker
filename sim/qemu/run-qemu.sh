#!/usr/bin/env bash
# File: run-qemu.sh
# Description: Boots Raspberry Pi OS arm64 in QEMU system emulation with
#              port-forwarding for SSH (:2222) and the kiosk UI (:8000).
#              Used for Path A (QEMU full-OS rehearsal) in sim/README.md.
# Project: smart_locker/sim/qemu
# Notes:
#   - QEMU does NOT emulate the ACR1252U reader.  Card taps come from the
#     project's fake reader (SMART_LOCKER_FAKE_READER=1) via POST /api/dev/tap,
#     the F2 key, or the corner button in the browser.
#   - QEMU does NOT validate GPU smoothness — kiosk perf testing requires real Pi hardware.
#   - Downloading the Pi OS image is a MANUAL step (see STEP 0 in sim/README.md).
#   - Tested against QEMU 7.x and 8.x; the raspi4b machine type requires QEMU >= 6.2.
#
# Usage (from the repo root or sim/qemu/):
#   bash sim/qemu/run-qemu.sh [IMAGE_FILE] [KERNEL_FILE] [DTB_FILE]
#
# Defaults (override via env or CLI args):
#   IMAGE  = raspios-bookworm-arm64-lite.img  (in the current directory)
#   KERNEL = kernel8.img                      (extracted from image boot partition)
#   DTB    = bcm2711-rpi-4-b.dtb             (extracted from image boot partition)
set -euo pipefail

# ---------------------------------------------------------------------------
# Prerequisites check
# ---------------------------------------------------------------------------
if ! command -v qemu-system-aarch64 >/dev/null 2>&1; then
  cat >&2 <<EOF
ERROR: qemu-system-aarch64 not found.

Install on Debian/Ubuntu:
  sudo apt-get install qemu-system-arm qemu-utils

Install on macOS (Homebrew):
  brew install qemu

Install on Fedora/RHEL:
  sudo dnf install qemu-system-aarch64

QEMU >= 6.2 is required for the raspi4b machine type.
EOF
  exit 1
fi

QEMU_VERSION=$(qemu-system-aarch64 --version | grep -oP '\d+\.\d+' | head -1)
echo "==> qemu-system-aarch64 version: $QEMU_VERSION"

# ---------------------------------------------------------------------------
# Configuration — override via CLI args or environment variables
# ---------------------------------------------------------------------------
IMAGE="${1:-${QEMU_IMAGE:-raspios-bookworm-arm64-lite.img}}"
KERNEL="${2:-${QEMU_KERNEL:-kernel8.img}}"
DTB="${3:-${QEMU_DTB:-bcm2711-rpi-4-b.dtb}}"

# Host ports forwarded to QEMU guest ports :22 (SSH) and :8000 (kiosk)
HOST_SSH_PORT="${QEMU_SSH_PORT:-2222}"
HOST_APP_PORT="${QEMU_APP_PORT:-8000}"

# RAM and CPU count
QEMU_RAM="${QEMU_RAM:-2048}"       # MB; Pi 4 has 2-8 GB depending on variant
QEMU_SMP="${QEMU_SMP:-4}"          # Pi 4 has 4 ARM Cortex-A72 cores

# ---------------------------------------------------------------------------
# Verify required files exist
# ---------------------------------------------------------------------------
missing=0
for f in "$IMAGE" "$KERNEL" "$DTB"; do
  if [ ! -f "$f" ]; then
    echo "MISSING: $f" >&2
    missing=1
  fi
done
if [ "$missing" -eq 1 ]; then
  cat >&2 <<'EOF'

One or more required files are missing. See sim/README.md Step A1 for how to
obtain and extract them. Quick reference:

  # Download Pi OS arm64 lite image (official Raspberry Pi Foundation):
  # URL pattern:
  #   https://downloads.raspberrypi.com/raspios_lite_arm64/images/
  #   raspios_lite_arm64-YYYY-MM-DD/YYYY-MM-DD-raspios-bookworm-arm64-lite.img.xz
  # Decompress:
  #   xz -d *.img.xz            # or: unxz *.img.xz

  # Mount the boot partition (partition 1) and copy kernel + dtb:
  OFFSET=$(fdisk -l raspios-bookworm-arm64-lite.img \
            | awk '/FAT32/{print $2 * 512}' | head -1)
  sudo mount -o loop,offset=$OFFSET raspios-bookworm-arm64-lite.img /mnt/tmp
  cp /mnt/tmp/kernel8.img .
  cp /mnt/tmp/bcm2711-rpi-4-b.dtb .
  sudo umount /mnt/tmp

  # Or on macOS use hdiutil to attach then copy from the volume.
EOF
  exit 1
fi

# ---------------------------------------------------------------------------
# Kernel command line (mirrors cmdline.txt in the official Pi OS image)
# ---------------------------------------------------------------------------
KERNEL_CMDLINE="console=serial0,115200 console=tty1 root=/dev/mmcblk0p2 rootfstype=ext4 fsck.repair=yes rootwait quiet"

# ---------------------------------------------------------------------------
# QEMU launch
# ---------------------------------------------------------------------------
echo ""
echo "==> Booting Raspberry Pi OS arm64 in QEMU"
echo "    image  : $IMAGE"
echo "    kernel : $KERNEL"
echo "    dtb    : $DTB"
echo "    RAM    : ${QEMU_RAM} MB"
echo "    SMP    : ${QEMU_SMP} cores"
echo "    SSH    : ssh -p ${HOST_SSH_PORT} pi@localhost"
echo "    kiosk  : http://localhost:${HOST_APP_PORT}"
echo ""
echo "    REMINDER: QEMU does NOT emulate the ACR1252U reader."
echo "    Inject taps via POST /api/dev/tap or press F2 in the browser."
echo ""

# Machine: raspi4b (QEMU 6.2+) — closest emulation to Pi 4 hardware.
#
# Network: user-mode (no root needed). Port-forwards 22->2222 and 8000->HOST_APP_PORT.
# The Pi 4's Gigabit Ethernet is emulated via the genet controller in raspi4b.
# If networking fails inside the guest, try adding: -device usb-net,netdev=net0
#
# Display: nographic = headless + serial console on stdio. Remove -nographic and
# add -display gtk (or -display sdl) for a graphical window.
#
# SD card: the Pi OS image is attached as an SD card (if=sd).

exec qemu-system-aarch64 \
  -machine raspi4b \
  -cpu cortex-a72 \
  -m "${QEMU_RAM}" \
  -smp "${QEMU_SMP}" \
  -kernel "${KERNEL}" \
  -dtb    "${DTB}" \
  -drive  "file=${IMAGE},format=raw,if=sd" \
  -append "${KERNEL_CMDLINE}" \
  -netdev "user,id=net0,hostfwd=tcp::${HOST_SSH_PORT}-:22,hostfwd=tcp::${HOST_APP_PORT}-:8000" \
  -device usb-net,netdev=net0 \
  -no-reboot \
  -nographic \
  "$@"

# ---------------------------------------------------------------------------
# Alternative: -machine virt (if raspi4b is not supported by your QEMU build)
# ---------------------------------------------------------------------------
# Replace the qemu-system-aarch64 invocation above with:
#
#   qemu-system-aarch64 \
#     -machine virt \
#     -cpu cortex-a72 \
#     -m "${QEMU_RAM}" \
#     -smp "${QEMU_SMP}" \
#     -kernel "${KERNEL}" \
#     -append "${KERNEL_CMDLINE} earlycon=pl011,0x9000000" \
#     -drive  "file=${IMAGE},format=raw,if=virtio" \
#     -netdev "user,id=net0,hostfwd=tcp::${HOST_SSH_PORT}-:22,hostfwd=tcp::${HOST_APP_PORT}-:8000" \
#     -device virtio-net-pci,netdev=net0 \
#     -no-reboot \
#     -nographic
#
# NOTE: The virt machine uses virtio block (not SD), so the root device changes
# from /dev/mmcblk0p2 to /dev/vda2. Edit KERNEL_CMDLINE accordingly.
# Also: the Pi OS kernel (kernel8.img) may not have virtio drivers built in;
# in that case use a generic arm64 Ubuntu or Debian cloud image instead.
