#!/usr/bin/env python3
"""Generate fake-PII text/CSV fixtures for handler tests.

Writes one fixture per extension handled by text_handler and csv_handler
into the directory given as argv[1]. Every fixture plants (where the
format allows) the spec's values:

  email  planted.email@example.com
  phone  (555) 010-9999
  ssn    000-55-4444
  name   Casey Plantedname          (custom-term check)
  keep   $12,345.67                 (must SURVIVE redaction)

All values are fake. Pure Python, no network. See
docs/plans/handler-spec.md ("Fixture generators").
"""

import csv
import json
import sys
from pathlib import Path

EMAIL = "planted.email@example.com"
PHONE = "(555) 010-9999"
SSN = "000-55-4444"
SSN9 = "000554444"  # unformatted: only detectable via a labeled column
NAME = "Casey Plantedname"
KEEP = "$12,345.67"


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    def write(name: str, content: str) -> None:
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path}")

    write("fixture.txt",
          f"Account statement for {NAME}\n"
          f"Contact email: {EMAIL}\n"
          f"Phone: {PHONE}\n"
          f"SSN: {SSN}\n"
          f"Closing balance: {KEEP}\n")

    write("fixture.md",
          f"# Statement for {NAME}\n\n"
          f"- Email: [{EMAIL}](mailto:{EMAIL})\n"
          f"- Phone: {PHONE}\n"
          f"- SSN: `{SSN}`\n\n"
          f"Closing balance: **{KEEP}**\n")

    write("fixture.json", json.dumps({
        "customer": {
            "name": NAME,
            "email": EMAIL,
            "phone": PHONE,
            "ssn": SSN,
        },
        "closing_balance": KEEP,
    }, indent=2) + "\n")

    write("fixture.yaml",
          "customer:\n"
          f"  name: {NAME}\n"
          f"  email: {EMAIL}\n"
          f'  phone: "{PHONE}"\n'
          f'  ssn: "{SSN}"\n'
          f'closing_balance: "{KEEP}"\n')

    write("fixture.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          "<statement>\n"
          f"  <name>{NAME}</name>\n"
          f"  <email>{EMAIL}</email>\n"
          f"  <phone>{PHONE}</phone>\n"
          f"  <ssn>{SSN}</ssn>\n"
          f"  <closingBalance>{KEEP}</closingBalance>\n"
          "</statement>\n")

    # Plants the email/phone inside href attributes too, exercising the
    # HTML link-target scan in text_handler — including entity-encoded
    # (&#64;) and percent-encoded (%40) spellings of the email, which only
    # the decoded-variant scan can find.
    write("fixture.html",
          "<html><head><title>Statement</title></head><body>\n"
          f"<h1>Statement for {NAME}</h1>\n"
          f'<p>Email: <a href="mailto:{EMAIL}">{EMAIL}</a></p>\n'
          f'<p>Alt: <a href="mailto:{EMAIL.replace("@", "%40")}">'
          f'{EMAIL.replace("@", "&#64;")}</a></p>\n'
          f'<p>Phone: <a href="tel:{PHONE}">{PHONE}</a></p>\n'
          f"<p>SSN: {SSN}</p>\n"
          f"<p>Closing balance: {KEEP}</p>\n"
          "</body></html>\n")

    # CSV/TSV: written via csv.writer so the quoted-comma cell and the
    # embedded-newline cell are quoted correctly per RFC 4180. The "SSN"
    # column holds an UNFORMATTED 9-digit value that label-based detectors
    # only find through csv_handler's header-as-context scan.
    rows = [
        ["name", "email", "phone", "ssn", "SSN", "address", "balance"],
        # quoted cell containing a comma                embedded newline cell
        [f"Plantedname, Casey ({NAME})", EMAIL, PHONE, SSN, SSN9,
         "123 Fake St\nSpringfield, XX 00000", KEEP],
    ]
    for name, delim in (("fixture.csv", ","), ("fixture.tsv", "\t")):
        path = out_dir / name
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter=delim).writerows(rows)
        print(f"wrote {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} OUTPUT_DIR")
    main(Path(sys.argv[1]))
