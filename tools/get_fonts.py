"""Download free fonts that documents often use, so new text can be written in the document's own font
(or a free twin with the same letter widths) when the PDF's embedded copy can't be used.

    python tools/get_fonts.py [target folder]        (the Docker build uses /usr/local/share/fonts/pdf-edit)

Every file is pinned (Google Fonts at a fixed commit, Selawik 1.01) and checked against its SHA-256, so a
build always gets the same files. Variable fonts (one file holding every weight) are cut into ordinary
static files (Light ... ExtraBold, and italics): a PDF draws a variable font only in its default weight.
All are under the SIL Open Font License 1.1 (the licence files are saved next to the fonts):
Montserrat, Poppins, Raleway, Source Sans 3, Merriweather, Playfair Display, Nunito, Oswald, Work Sans,
PT Sans, PT Serif; Gelasio (same letter widths as Georgia); Selawik (Microsoft's twin of Segoe UI).
"""
import hashlib
import io
import os
import sys
import urllib.parse
import urllib.request
import zipfile

GOOGLE = "https://raw.githubusercontent.com/google/fonts/bd8f81ddb5c74d5c8897b36ad88b440266245103/"
SELAWIK = "https://github.com/microsoft/Selawik/releases/download/1.01/Selawik_Release.zip"
SELAWIK_LICENSE = "https://raw.githubusercontent.com/microsoft/Selawik/1.01/LICENSE.txt"

# (path in the Google Fonts repository, SHA-256, family name for the files)
FILES = [
    ("ofl/montserrat/Montserrat[wght].ttf", "0f7b311b2f3279e4eef9b2f968bcdbab6e28f4daeb1f049f4f278a902bcd82f7", "Montserrat"),
    ("ofl/montserrat/Montserrat-Italic[wght].ttf", "51607f316bc020e59f03cbf51543eecffbea501c0b31d73e5b82927c5cca442c", "Montserrat"),
    ("ofl/montserrat/OFL.txt", "8b7141c03fa4f8d44e6345d5d4931709290f0f67875e452e95ac1fd3a027802e", "Montserrat"),
    ("ofl/poppins/Poppins-Light.ttf", "650ba57fa99d12ec40c31ccfb680be656be4497fbe14164617d67e32ffe9cd46", "Poppins"),
    ("ofl/poppins/Poppins-LightItalic.ttf", "b8f9c5be59723fadf8e5447fa1245c2c53b60a3464a24d6ece9ee3c283d8917b", "Poppins"),
    ("ofl/poppins/Poppins-Regular.ttf", "7e65201e9b79159e2300267cc885e16c8dcef2424cdfa09a29bfb0980a94a7ba", "Poppins"),
    ("ofl/poppins/Poppins-Italic.ttf", "4fa76ae75b40f926420514044722cb97f32186cafd3b38263cc34dad7174d46d", "Poppins"),
    ("ofl/poppins/Poppins-Medium.ttf", "90373e7d838d32468438fc3e152dca0bdb12edcab99ea639f158790b1ba1fd05", "Poppins"),
    ("ofl/poppins/Poppins-MediumItalic.ttf", "765addf6c7c11ec3c54325cacd68cabd05df8e4b6455302d812a9b9bafd1c614", "Poppins"),
    ("ofl/poppins/Poppins-SemiBold.ttf", "d3bf1bdaf0550e83da9ac0b1d1d9fe6db086835a83aa28578e609a394b9a0286", "Poppins"),
    ("ofl/poppins/Poppins-SemiBoldItalic.ttf", "16bb118aa232c9a13fa238027d24d7854dd1a1d9cbaf99b17fec4388d56b432c", "Poppins"),
    ("ofl/poppins/Poppins-Bold.ttf", "983676516167748b74de6f4771fb384c664fd913acb8b471122ecacf5da5ea6c", "Poppins"),
    ("ofl/poppins/Poppins-BoldItalic.ttf", "3572ac8116a0ac7317d342262b29937bcbaf94d8f03f90df6fe666fa7e2fb43a", "Poppins"),
    ("ofl/poppins/Poppins-ExtraBold.ttf", "f2ab17c1a63a0ecc12c2461848fc8a469395e3cd2d641803e889c643d9f958e1", "Poppins"),
    ("ofl/poppins/Poppins-ExtraBoldItalic.ttf", "dc00a2eb988373e9f4e99bf8ff76c6315ee21d36b341cbbff024e4c18cc7ac03", "Poppins"),
    ("ofl/poppins/OFL.txt", "6be04893d770899a015649c7aa3b582f871b272f8747a92b78b17c3e5c8b2573", "Poppins"),
    ("ofl/raleway/Raleway[wght].ttf", "8bbcc3eb8275c388f4bcd998832f8a4b943eadbaf6a595205312774b5951aefb", "Raleway"),
    ("ofl/raleway/Raleway-Italic[wght].ttf", "96629caf2202183fab46c70237055a7d67e6a5400b85413d45a77ed6f2a0770c", "Raleway"),
    ("ofl/raleway/OFL.txt", "7e946cf1171784d1015279e7dc35f827957a6b5d1f1f659ae0c98e5f5e37ed9b", "Raleway"),
    ("ofl/sourcesans3/SourceSans3[wght].ttf", "042fe2cc0b933e328410d7acbd0aa6a1873dca5aef81875f4bc214b08825c7b9", "Source Sans 3"),
    ("ofl/sourcesans3/SourceSans3-Italic[wght].ttf", "39e3ab05ccd7cb94907c31005bb5bec1d5432f0b096a2b782976e217a540eb6c", "Source Sans 3"),
    ("ofl/sourcesans3/OFL.txt", "09746787287a289323b0ec3cff4d1a4a801331b82b7207c1e186f5d26619a392", "Source Sans 3"),
    ("ofl/merriweather/Merriweather[opsz,wdth,wght].ttf", "d0ed0e359e396af7ad05e73dffd11a3a4c326ea0d0283c56bd9361cb2cc86a96", "Merriweather"),
    ("ofl/merriweather/Merriweather-Italic[opsz,wdth,wght].ttf", "f68a8f4989258679e4fbaf50aa42400132b5373c2d9d2514ba82ef6e85947a0b", "Merriweather"),
    ("ofl/merriweather/OFL.txt", "715bd349bfa6116f99e351e1548321dcfa25b91df65502d22ca8d624557697d2", "Merriweather"),
    ("ofl/playfairdisplay/PlayfairDisplay[wght].ttf", "c40f2293766a503bc70cce9e512ef844a4ccb7cbcde792fe2ea31d191917d8d6", "Playfair Display"),
    ("ofl/playfairdisplay/PlayfairDisplay-Italic[wght].ttf", "a5e26dc5e2e77fb2803a0bf02fd4f81ee136ec8dea863ccdb0c59a263b21378b", "Playfair Display"),
    ("ofl/playfairdisplay/OFL.txt", "566be814f8e96e93dfa16101331557eb6b5467e9e03f627c0910fe93ca12300e", "Playfair Display"),
    ("ofl/nunito/Nunito[wght].ttf", "bb55a5ca5c2042335b3991af27c4d0705d0ef41cac6164ac737fd8f2a1e85207", "Nunito"),
    ("ofl/nunito/Nunito-Italic[wght].ttf", "b520cc871868b0acfca1beda875df7f4a44ebce914f8a89f83977fc9c09529c8", "Nunito"),
    ("ofl/nunito/OFL.txt", "580df76c95a1ec5ab878ceb25bb3d85c6a076804e9c970c8c6972aea775fdf65", "Nunito"),
    ("ofl/oswald/Oswald[wght].ttf", "5b38c246e255a12f5712d640d56bcced0472466fc68983d2d0410ec0457c2817", "Oswald"),
    ("ofl/oswald/OFL.txt", "0fd731a904b729a4e02eaf5e8ebd06783edd9abe400e8882760160230675b652", "Oswald"),
    ("ofl/gelasio/Gelasio[wght].ttf", "4daecea457258c9ebeb8bc99ed3fd24353618bfad3ea4b93fa0b5d0468fc04e4", "Gelasio"),
    ("ofl/gelasio/Gelasio-Italic[wght].ttf", "52559e845a4d33514e5f93bb9ae7dbeae1894a53f2c565a15f18af40cd337c09", "Gelasio"),
    ("ofl/gelasio/OFL.txt", "b393cb01867c919b44381512120dc3e4c954c7b47e2035c405f3a324799a4d29", "Gelasio"),
    ("ofl/worksans/WorkSans[wght].ttf", "f50f61f2ba738e239442d40bf1069adb195c224b6a5a73a581fc2f3ed62a9f63", "Work Sans"),
    ("ofl/worksans/WorkSans-Italic[wght].ttf", "0a9f935ea490d3477fc97e40248f356c29bce11a1973939056c4316b122341ec", "Work Sans"),
    ("ofl/worksans/OFL.txt", "749aca05078664ce682dce1b1b10096ac397cb088c1a6df4e1bb56f0092a9272", "Work Sans"),
    ("ofl/ptsans/PT_Sans-Web-Regular.ttf", "9cc831490532009bae2b3ce0d39c62adfc889060beb421593bfd9d2396d0f10a", "PT Sans"),
    ("ofl/ptsans/PT_Sans-Web-Italic.ttf", "5a90fe2d0cd798700935240580bdcc12c0ffc9102c0c7163b3418e13bc21debd", "PT Sans"),
    ("ofl/ptsans/PT_Sans-Web-Bold.ttf", "3128bd5ecf01816e59a23d54c57a7a6b14615b07db53ff277c77376010265b05", "PT Sans"),
    ("ofl/ptsans/PT_Sans-Web-BoldItalic.ttf", "81ac221cdd02bccfa679c74adb122478e9d092e65a722e31ca11469961483785", "PT Sans"),
    ("ofl/ptsans/OFL.txt", "2758cf7a872827f39661cf8cc24188113c030447aefb5ca7145993650076ca8c", "PT Sans"),
    ("ofl/ptserif/PT_Serif-Web-Regular.ttf", "a4951fade06ff8f09b7673aa81ffb65a8cd409e24d3289a6dc670bc4dda2557a", "PT Serif"),
    ("ofl/ptserif/PT_Serif-Web-Italic.ttf", "f57e95ff9dc85691a3b2e193f2028db36f6663939a46c0fc4f286d618b80b7ce", "PT Serif"),
    ("ofl/ptserif/PT_Serif-Web-Bold.ttf", "038ba7336bd7ea14f12ad155bed51a4345cac5153275d521dec3ba04021c526e", "PT Serif"),
    ("ofl/ptserif/PT_Serif-Web-BoldItalic.ttf", "f003788ba08981eb0988b3557a6f224a53dab49c20e283e8b74d5af3c466f8be", "PT Serif"),
    ("ofl/ptserif/OFL.txt", "ddf311c28ddf5a5ad9747649837346b67bed9d356789c3072bb27dbce49e514d", "PT Serif"),
]
SELAWIK_SHA = "3f62c51e05e3b5a1e6241cf92a371f0be2ea1183aa87b30718bbd40832a8d423"
SELAWIK_LICENSE_SHA = "77b7c2506d4efb22e09c8ccf10159f4956eab3ef7c007fef95de136bcf45300c"
SELAWIK_FILES = {"selawk.ttf": "Regular", "selawkb.ttf": "Bold", "selawksb.ttf": "SemiBold",
                 "selawkl.ttf": "Light", "selawksl.ttf": "SemiLight"}

WEIGHTS = [(300, "Light"), (400, "Regular"), (500, "Medium"), (600, "SemiBold"), (700, "Bold"), (800, "ExtraBold")]


def fetch(url, sha):
    data = urllib.request.urlopen(url, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != sha:
        sys.exit(f"Checksum mismatch for {url}: refusing to use it.")
    return data


def style_name(weight_name, italic):
    if italic:
        return "Italic" if weight_name == "Regular" else weight_name + "Italic"
    return weight_name


def set_names(font, family, style, weight, italic):
    """Name a static font like an ordinary one: "Montserrat SemiBold", PostScript "Montserrat-SemiBold"."""
    name = font["name"]
    for nid in (1, 2, 3, 4, 6, 16, 17, 21, 22, 25):
        name.removeNames(nameID=nid)
    sub = {"Regular": "Regular", "Italic": "Italic", "Bold": "Bold", "BoldItalic": "Bold Italic"}.get(style)
    fam1, sub1 = (family, sub) if sub else (f"{family} {style.replace('Italic', '').strip() or 'Regular'}",
                                           "Italic" if italic else "Regular")
    full = f"{family} {' '.join(w for w in [style.replace('Italic', ''), 'Italic' if italic else ''] if w) or 'Regular'}"
    ps = f"{family.replace(' ', '')}-{style}"
    for nid, value in ((1, fam1), (2, sub1), (3, ps), (4, full), (6, ps), (16, family),
                       (17, full[len(family) + 1:])):
        name.setName(value, nid, 3, 1, 0x409)
        name.setName(value, nid, 1, 0, 0)
    if "OS/2" in font:
        font["OS/2"].usWeightClass = weight
        sel = font["OS/2"].fsSelection & ~0b1100001
        font["OS/2"].fsSelection = sel | (0x01 if italic else 0) | (0x20 if weight >= 700 else 0) | \
            (0x40 if (weight == 400 and not italic) else 0)
    font["head"].macStyle = (1 if weight >= 700 else 0) | (2 if italic else 0)


def save_static(data, family, italic, target):
    """Cut a variable font into static files, one per weight it covers; return how many."""
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
    var = TTFont(io.BytesIO(data))
    axes = {a.axisTag: a for a in var["fvar"].axes}
    n = 0
    for weight, wname in WEIGHTS:
        a = axes["wght"]
        if not a.minValue <= weight <= a.maxValue:
            continue
        pin = {tag: ax.defaultValue for tag, ax in axes.items()}
        pin["wght"] = weight
        font = instancer.instantiateVariableFont(TTFont(io.BytesIO(data)), pin, optimize=False)
        style = style_name(wname, italic)
        set_names(font, family, style, weight, italic)
        font.save(os.path.join(target, f"{family.replace(' ', '')}-{style}.ttf"))
        n += 1
    return n


def main(target):
    os.makedirs(target, exist_ok=True)
    count = 0
    for path, sha, family in FILES:
        data = fetch(GOOGLE + urllib.parse.quote(path), sha)
        base = os.path.basename(path)
        if base == "OFL.txt":
            with open(os.path.join(target, f"{family.replace(' ', '')}-OFL.txt"), "wb") as f:
                f.write(data)
        elif "[" in base:
            count += save_static(data, family, "Italic" in base, target)
        else:
            with open(os.path.join(target, base), "wb") as f:
                f.write(data)
            count += 1
    z = zipfile.ZipFile(io.BytesIO(fetch(SELAWIK, SELAWIK_SHA)))
    for member in SELAWIK_FILES:
        with open(os.path.join(target, member), "wb") as f:
            f.write(z.read(member))
        count += 1
    with open(os.path.join(target, "Selawik-OFL.txt"), "wb") as f:
        f.write(fetch(SELAWIK_LICENSE, SELAWIK_LICENSE_SHA))
    print(f"{count} font files in {target}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/usr/local/share/fonts/pdf-edit")
