"""Download the emoji pictures the editor draws (Twemoji 15.0, about 3,700 SVG files, 8 MB) into ./emoji.

    python tools/get_emoji.py [target folder]

The package is pinned and checked against its published checksum, so a build always gets the same files.
Emoji graphics: Twemoji, (c) Twitter/X and other contributors, CC-BY 4.0 (https://github.com/jdecked/twemoji).
"""
import base64
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

URL = "https://registry.npmjs.org/@twemoji/svg/-/svg-15.0.0.tgz"
SHA512 = "ZSPef2B6nBaYnfgdTbAy4jgW95o7pi2xPGwGCU+WMTxo7J6B1lMPTWwSq/wTuiMq+N0khQ90CcvYp1wFoQpo/w=="


def main(target):
    data = urllib.request.urlopen(URL, timeout=60).read()
    got = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if got != SHA512:
        sys.exit(f"Checksum mismatch for {URL}: refusing to use it.")
    os.makedirs(target, exist_ok=True)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            name = os.path.basename(m.name)
            # only plain files named like "1f44d-1f3fd.svg"; nothing else is written
            if m.isfile() and name.endswith(".svg") and all(c in "0123456789abcdef-." for c in name[:-4]) and "/" not in name:
                with open(os.path.join(target, name), "wb") as f:
                    f.write(tar.extractfile(m).read())
                n += 1
    print(f"{n} emoji pictures in {target}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "emoji"))
