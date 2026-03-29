"""
File: generate_key.py
Description: One-time key generation utility. Generates two 32-byte keys
             (AES-256 encryption + HMAC-SHA256) as base64 strings suitable
             for pasting into a .env file.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.generate_key
"""

import base64
import os


def main() -> None:
    """Generate a pair of cryptographic keys and print them for .env configuration.

    Creates two 32-byte (256-bit) random keys — one for AES-256-GCM encryption
    and one for HMAC-SHA256 hashing — encoded as base64 strings ready to paste
    into the project's .env file.

    Returns:
        None. Keys are printed to stdout.
    """
    # 32 bytes = 256 bits, required for AES-256-GCM and HMAC-SHA256
    enc_key = base64.b64encode(os.urandom(32)).decode("ascii")
    hmac_key = base64.b64encode(os.urandom(32)).decode("ascii")

    print("Add these to your .env file:\n")
    print(f"SMART_LOCKER_ENC_KEY={enc_key}")
    print(f"SMART_LOCKER_HMAC_KEY={hmac_key}")


if __name__ == "__main__":
    main()
