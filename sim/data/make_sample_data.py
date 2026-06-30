"""
File: make_sample_data.py
Description: Generates sim/data/Messmittelliste.sample.xlsx and sim/data/photos/
             placeholder images for the Smart Locker simulation harness.
             Column headers are chosen to match the auto-detector in
             smart_locker/sync/source_import.py (German + English candidates).
Project: smart_locker/sim/data
Notes: Run from the repo root or from sim/data/:
         python sim/data/make_sample_data.py
       Requires openpyxl (already in requirements.txt).
       Pillow is used for photos if installed; falls back to a hardcoded
       minimal valid JPEG (1x1 white pixel) when Pillow is absent.
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — resolve relative to this script so it works from any cwd
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent          # sim/data/
PHOTOS_DIR = HERE / "photos"
XLSX_PATH = HERE / "Messmittelliste.sample.xlsx"

# ---------------------------------------------------------------------------
# Column headers
# ---------------------------------------------------------------------------
# Each header is chosen to match one of the candidate lists in source_import.py
# so the auto-detector picks up the right column without manual config.
#
#   HEADER                 -> candidate list in source_import.py
#   "Inventarnummer"       -> PM_CANDIDATES        ("inventarnummer")
#   "Name"                 -> NAME_CANDIDATES       ("name")
#   "Platz Messmittelschrank" -> SLOT_CANDIDATES  ("platz messmittelschrank")
#   "Hersteller"           -> MANUFACTURER_CANDIDATES ("hersteller")
#   "Typbezeichnung"       -> MODEL_CANDIDATES     ("typbezeichnung")
#   "Seriennummer"         -> SERIAL_CANDIDATES    ("seriennummer")
#   "Aktueller Einsatzort" -> LOCATION_CANDIDATES  ("aktueller einsatzort")
#
# "Aktueller Einsatzort" is the ONLY entry in LOCATION_CANDIDATES — the header
# must match exactly (case-insensitive). All others have multiple synonyms.

HEADERS = [
    "Inventarnummer",
    "Name",
    "Platz Messmittelschrank",
    "Hersteller",
    "Typbezeichnung",
    "Seriennummer",
    "Aktueller Einsatzort",
]

# ---------------------------------------------------------------------------
# Device rows
# ---------------------------------------------------------------------------
# "Platz Messmittelschrank" starts with "Schrank" -> imported (schrank rows).
# "Aktueller Einsatzort" contains "schrank"       -> status AVAILABLE.
# "Aktueller Einsatzort" = person name            -> status BORROWED.
# "Platz Messmittelschrank" does NOT start with "schrank" -> skipped.
#
# Format: (Inventarnummer, Name, Slot, Hersteller, Typbezeichnung, Seriennummer, Einsatzort)

ROWS = [
    # --- Schrank rows: imported -------------------------------------------
    # AVAILABLE (Einsatzort contains "schrank")
    ("1001", "Digital-Multimeter",      "Schrank A1", "Fluke", "87V",            "SN-FL-001", "Schrank A1"),
    ("1002", "Scopemeter",              "Schrank A2", "Fluke", "ScopeMeter 120B", "SN-FL-002", "Schrank A2"),
    ("1003", "Kabeltester",             "Schrank A3", "Fluke", "MicroScanner2",  "SN-FL-003", "Schrank A3"),
    ("1006", "Temperaturdatenlogger",   "Schrank A6", "Testo", "175T1",          "SN-TE-006", "Schrank A6"),
    # BORROWED (Einsatzort = person name, non-empty, no "schrank")
    ("1004", "Zangenamperemeter",       "Schrank A4", "Fluke", "376 FC",         "SN-FL-004", "Max Mustermann"),
    ("1005", "Isolationsprüfer",        "Schrank A5", "Fluke", "1587 FC",        "SN-FL-005", "Anna Schmidt"),

    # --- Non-schrank rows: skipped (non_locker_skipped) -------------------
    ("1007", "Drehmomentschlüssel",     "Regal B1",   "Gedore", "2642-05",       "SN-GE-007", "Regal B1"),
    ("1008", "Etikettendrucker",        "",           "Brady",  "M611",          "SN-BR-008", ""),
]

# Photo placeholders are created for the first two schrank model names:
# "87V" -> 87V.jpg  (exact model match; Fluke 87V row)
# "175T1" -> 175T1.jpg (exact model match; Testo 175T1 row)
PHOTO_MODELS = ["87V", "175T1"]

# ---------------------------------------------------------------------------
# Minimal 1x1 white JPEG fallback (used when Pillow is not installed)
# ---------------------------------------------------------------------------
# This is a standard-compliant JFIF JPEG encoding a single white pixel.
# Browsers, openpyxl, and Python's imghdr all recognise it as a valid JPEG.
_MINIMAL_WHITE_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
    b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f'
    b'\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\x1e'
    b'\x1b\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4'
    b'\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00'
    b'\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4'
    b'\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00'
    b'\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x14'
    b'2\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18'
    b'\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85'
    b'\x86\x87\x88\x89\x8a\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4'
    b'\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2'
    b'\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9'
    b'\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5'
    b'\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2'
    b'\xff\xd9'
)


def _write_photo(model: str) -> Path:
    """Write a tiny placeholder JPEG for the given model name.

    Tries Pillow first (coloured 32x32 image with the model name as text);
    falls back to a hardcoded minimal 1x1 white JPEG when Pillow is absent.

    Args:
        model: Device model string — becomes the filename stem (e.g. "87V"
               -> "87V.jpg"), matching the photo-matching rule in settings.py.

    Returns:
        Path to the written JPEG file.
    """
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    dest = PHOTOS_DIR / f"{model}.jpg"
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 32), color=(210, 230, 210))
        draw = ImageDraw.Draw(img)
        draw.text((4, 8), model, fill=(40, 80, 40))
        img.save(dest, "JPEG", quality=70)
        print(f"  [Pillow] photo: {dest.name} (64x32 px)")
    except ImportError:
        dest.write_bytes(_MINIMAL_WHITE_JPEG)
        print(f"  [fallback JPEG] photo: {dest.name} (1x1 px, {len(_MINIMAL_WHITE_JPEG)} bytes)")
    return dest


def _write_xlsx() -> dict:
    """Write the sample workbook and return row-count stats.

    Returns:
        dict with keys "schrank", "borrowed", "available", "skipped", "total".
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        print("ERROR: openpyxl not installed. Run: pip install openpyxl")
        sys.exit(1)

    wb = Workbook()
    ws = wb.active
    ws.title = "Messmittel"

    # Header row — styled to look like a real sheet
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
    for col_idx, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    # Data rows
    for row_idx, row_data in enumerate(ROWS, start=2):
        for col_idx, value in enumerate(row_data, start=1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    # Column widths for readability
    widths = [14, 26, 22, 12, 18, 14, 20]
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width

    wb.save(XLSX_PATH)

    # Count stats for verification
    schrank_rows = [r for r in ROWS if str(r[2]).lower().startswith("schrank")]
    available   = [r for r in schrank_rows if "schrank" in str(r[6]).lower()]
    borrowed    = [r for r in schrank_rows if r[6] and "schrank" not in str(r[6]).lower()]
    skipped     = [r for r in ROWS if not str(r[2]).lower().startswith("schrank")]

    return {
        "total":     len(ROWS),
        "schrank":   len(schrank_rows),
        "available": len(available),
        "borrowed":  len(borrowed),
        "skipped":   len(skipped),
    }


def main() -> None:
    """Entry point — generate the workbook and photo placeholders."""
    print("==> make_sample_data.py")
    print(f"    output xlsx  : {XLSX_PATH}")
    print(f"    output photos: {PHOTOS_DIR}/")
    print()

    stats = _write_xlsx()
    print(f"  workbook written: {XLSX_PATH.name}")
    print(f"    total rows     : {stats['total']}")
    print(f"    schrank rows   : {stats['schrank']}  (imported)")
    print(f"      -> available : {stats['available']}")
    print(f"      -> borrowed  : {stats['borrowed']}")
    print(f"    non-schrank    : {stats['skipped']}  (skipped by importer)")
    print()

    for model in PHOTO_MODELS:
        _write_photo(model)

    print()
    print("Done. Verify the sheet in any spreadsheet app, or run:")
    print("  python -m scripts.sync_source   # inside the running app's cwd")


if __name__ == "__main__":
    main()
