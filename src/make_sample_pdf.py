#!/usr/bin/env python3
"""
make_sample_pdf.py — generate fake test PDFs into the input/ folder.

Creates two files (ALL data is fake — 555 phone numbers, invalid SSN
ranges, made-up accounts):

  input/sample_statement.pdf  - searchable 2-page brokerage + medical doc
  input/sample_scanned.pdf    - image-only page (simulates a scan) to
                                exercise the OCR path

Then test the whole pipeline with:

    ./scripts/run.sh financial
"""

from pathlib import Path

import fitz  # PyMuPDF

PAGE1 = """\
ACME BROKERAGE SERVICES
Quarterly Statement — April 1 to June 30, 2026

John Q. Sample
123 Maple Grove Ave, Apt 4B
Springfield, IL 62704

Account Number: 4815-162342-99
Routing Number: 021000021
SSN: 000-12-3456
Tax ID: 12-3456789
DOB: 03/15/1980
Phone: (555) 867-5309
Email: john.sample@example.com

ACCOUNT SUMMARY
Beginning balance                             $124,532.18
Ending balance                                $131,908.44
Total gain/loss this period                     +$7,376.26

HOLDINGS
Symbol   Description            Qty      Price       Value
AAPL     Apple Inc.            150     $212.44   $31,866.00
VTI      Vanguard Total Mkt    420     $278.11  $116,806.20
Cost basis: $98,441.00     Unrealized gain: $33,465.20
Dividends paid 05/15/2026: $412.87    Fees: $25.00
"""

PAGE2 = """\
SPRINGFIELD MEDICAL CENTER — Visit Summary

Patient: John Q. Sample
MRN: A12345678
Member ID: XYZ-889-4412
Group Number: 55521
Date of Birth: March 15, 1980
Visit date: 06/12/2026

Provider: Dr. Jane Chen
Facility: 500 Oak Street, Springfield, IL 62704

Payment on file: card ending ****4321
Card number: 4111-1111-1111-1111
Driver's License: S530-1112-2233
Contact: 555-234-5678 / billing@springfieldmed.example.org

RE: Case No. 2026-CV-01847 (insurance dispute)
Amount billed: $1,250.00   Insurance paid: $1,000.00
"""

SCANNED = """\
FAKE UTILITY CO — FINAL NOTICE  (simulated scan)

Customer: John Q. Sample
Account Number: 998877665544
SSN: 000-98-7654
Phone: (555) 301-9988
Service address: 123 Maple Grove Ave, Springfield, IL 62704

Amount due: $142.19    Due date: 07/25/2026
"""


def make_qr_pixmap():
    """A QR code holding fake account data, to test barcode redaction.

    Returns a fitz.Pixmap, or None if the qrcode library isn't installed.
    """
    try:
        import io

        import qrcode
    except ImportError:
        return None
    img = qrcode.make("ACCT:4815-162342-99;SSN:000-12-3456 (fake)")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return fitz.Pixmap(buf.getvalue())


def main():
    root = Path(__file__).resolve().parent.parent
    input_dir = root / "input"
    input_dir.mkdir(exist_ok=True)

    # --- searchable sample -------------------------------------------------
    doc = fitz.open()
    for body in (PAGE1, PAGE2):
        page = doc.new_page()  # US Letter-ish default (612 x 792)
        page.insert_text((50, 60), body, fontname="courier",
                         fontsize=10, lineheight=1.4)
    qr = make_qr_pixmap()
    if qr is not None:
        # Bottom of page 1, like the scan-to-pay codes on real statements.
        doc[0].insert_text((50, 640), "Scan to view your account online:",
                           fontname="courier", fontsize=10)
        doc[0].insert_image(fitz.Rect(50, 650, 170, 770), pixmap=qr)
    doc.save(input_dir / "sample_statement.pdf")
    doc.close()

    # --- image-only sample (simulated scan) --------------------------------
    # Render a text page to a bitmap, then embed only the bitmap: the result
    # has NO text layer, just like a scanner output.
    tmp = fitz.open()
    page = tmp.new_page()
    page.insert_text((50, 80), SCANNED, fontname="courier",
                     fontsize=11, lineheight=1.5)
    pix = page.get_pixmap(dpi=200)
    tmp.close()

    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    doc.save(input_dir / "sample_scanned.pdf")
    doc.close()

    print(f"Wrote {input_dir}/sample_statement.pdf (searchable, 2 pages)")
    print(f"Wrote {input_dir}/sample_scanned.pdf   (image-only, tests OCR)")
    print("All data is fake. Try:  ./scripts/run.sh financial")


if __name__ == "__main__":
    main()
