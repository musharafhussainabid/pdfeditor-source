"""Extract the Chinese, Japanese and Korean fonts the editor uses from Debian's fonts-noto-cjk package.

    python tools/get_cjk.py [source folder] [target folder]

The package ships font collections (.ttc): one file holds the Simplified Chinese, Traditional Chinese,
Japanese, Korean and Hong Kong versions, and MuPDF's layout would always take the first (Japanese). The
versions the editor needs are saved as separate fonts: Noto Sans / Serif CJK SC, TC, JP, KR, Regular and
Bold (NotoSansCJKsc-Bold.otf ...). The Docker build removes the package afterwards. SIL Open Font License.
"""
import os
import sys

from fontTools.ttLib import TTCollection

REGIONS = {"SC": "sc", "TC": "tc", "JP": "jp", "KR": "kr"}


def main(src, target):
    os.makedirs(target, exist_ok=True)
    n = 0
    for family in ("Sans", "Serif"):
        for weight in ("Regular", "Bold"):
            path = os.path.join(src, f"Noto{family}CJK-{weight}.ttc")
            for font in TTCollection(path).fonts:
                name = font["name"].getDebugName(1) or ""          # e.g. "Noto Sans CJK SC"
                region = name.rsplit(" ", 1)[-1]
                if not name.startswith(f"Noto {family} CJK ") or region not in REGIONS:
                    continue                                       # skip Mono and the Hong Kong version
                font.save(os.path.join(target, f"Noto{family}CJK{REGIONS[region]}-{weight}.otf"))
                n += 1
    if n != 16:
        sys.exit(f"Expected 16 CJK fonts, found {n}: refusing to continue.")
    with open(os.path.join(target, "NotoCJK-OFL.txt"), "w") as f:
        f.write("Noto CJK fonts: Copyright 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name "
                "'Source'. Licensed under the SIL Open Font License, Version 1.1 (https://openfontlicense.org).\n")
    print(f"{n} CJK fonts in {target}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/opentype/noto",
         sys.argv[2] if len(sys.argv) > 2 else "/usr/local/share/fonts/pdf-edit")
