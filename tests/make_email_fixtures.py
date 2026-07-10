#!/usr/bin/env python3
"""
make_email_fixtures.py — generate a fake-PII .eml fixture, with a planted-
PII text attachment, for handler tests (docs/plans/expansion-plan.md §3.E).

Writes into the directory given as argv[1]:
  fixture.eml — headers (From/To/Subject) carry planted identifiers, body
  carries the rest, and one .txt attachment carries its own planted
  identifiers + the must-survive dollar amount — proving attachment
  recursion actually redacts the attachment, not just the email body.

NOTE: .msg (extract-msg) has no pure-Python writer available to build a
fixture with — see docs/plans/expansion-plan.md §3.E execution notes.
Only .eml is covered here.

Pure Python (stdlib email only), no network.
"""

from __future__ import annotations

import sys
from email.message import EmailMessage
from pathlib import Path

PLANTED_EMAIL = "planted.email@example.com"
PLANTED_PHONE = "(555) 010-9999"
PLANTED_SSN = "000-55-4444"
PLANTED_NAME = "Casey Plantedname"
SURVIVOR = "$12,345.67"


def make_eml(out_dir: Path) -> Path:
    path = Path(out_dir) / "fixture.eml"
    msg = EmailMessage()
    msg["From"] = f"{PLANTED_NAME} <{PLANTED_EMAIL}>"
    msg["To"] = "recipient@example.com"
    msg["Subject"] = f"Account review — call {PLANTED_PHONE}"
    msg.set_content(
        f"Please review the attached statement.\n"
        f"Taxpayer SSN on file: {PLANTED_SSN}\n"
        f"Balance: {SURVIVOR} (verified).\n"
    )
    attachment_text = (
        f"Attachment record for {PLANTED_NAME}\n"
        f"Contact: {PLANTED_EMAIL}\n"
        f"Closing balance: {SURVIVOR} (must survive)\n"
    )
    msg.add_attachment(
        attachment_text.encode("utf-8"),
        maintype="text", subtype="plain", filename="statement.txt",
    )
    path.write_bytes(msg.as_bytes())
    print(f"wrote {path}")
    return path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: make_email_fixtures.py <output-dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    make_eml(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
