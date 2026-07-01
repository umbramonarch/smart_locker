"""
File: sign_update.py
Description: Signs a release tarball with an HMAC-SHA256 sidecar so update.sh
             can verify it before applying it as root on the Pi.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.sign_update <path-to-tarball>
       Requires SMART_LOCKER_UPDATE_HMAC_KEY in .env (generate with
       'python -m scripts.generate_key') -- the same value must be in the
       Pi's .env, since update.sh verifies with that key. Writes
       <tarball>.hmac next to the tarball.
"""

import hashlib
import hmac
import os
import sys

from dotenv import load_dotenv

load_dotenv()

KEY_ENV_VAR = "SMART_LOCKER_UPDATE_HMAC_KEY"


def main() -> None:
    """Sign a release tarball with an HMAC-SHA256 sidecar.

    Reads the tarball path from argv, computes its HMAC-SHA256 using
    SMART_LOCKER_UPDATE_HMAC_KEY, and writes the hex digest to
    "<tarball>.hmac" next to it.

    Raises:
        SystemExit: If the argument count is wrong, the key is missing, or
            the tarball does not exist.
    """
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.sign_update <path-to-tarball>", file=sys.stderr)
        raise SystemExit(2)

    tarball_path = sys.argv[1]
    key = os.getenv(KEY_ENV_VAR)
    if not key:
        print(
            f"Missing {KEY_ENV_VAR} in .env — generate one with: python -m scripts.generate_key",
            file=sys.stderr,
        )
        raise SystemExit(1)

    with open(tarball_path, "rb") as f:
        digest = hmac.new(key.encode("utf-8"), f.read(), hashlib.sha256).hexdigest()

    sidecar_path = tarball_path + ".hmac"
    with open(sidecar_path, "w") as f:
        f.write(digest + "\n")

    print(f"Wrote {sidecar_path}")


if __name__ == "__main__":
    main()
