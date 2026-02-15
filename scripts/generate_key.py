"""One-time key generation utility.

Generates two 32-byte keys (encryption + HMAC) as base64 strings
suitable for pasting into a .env file.

Usage:
    python -m scripts.generate_key
"""

import base64
import os


def main() -> None:
    enc_key = base64.b64encode(os.urandom(32)).decode("ascii")
    hmac_key = base64.b64encode(os.urandom(32)).decode("ascii")

    print("Add these to your .env file:\n")
    print(f"SMART_LOCKER_ENC_KEY={enc_key}")
    print(f"SMART_LOCKER_HMAC_KEY={hmac_key}")


if __name__ == "__main__":
    main()
