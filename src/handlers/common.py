"""Shared pieces of the handler contract (see docs/plans/handler-spec.md)."""


class UnsupportedFormatError(Exception):
    """Raised when a file cannot be processed. The message is shown to the
    user and must say WHAT is unsupported and what to do instead."""


class EncryptedFileError(Exception):
    """Raised when a file is password-protected and could not be opened
    (missing or wrong password) — maps to exit code 5, distinct from
    UnsupportedFormatError (exit 4): the format IS supported, the file
    just couldn't be unlocked with what was given."""


def redaction_text(match: str) -> str:
    """Replacement string for a redacted span in text-based formats."""
    return "█" * min(len(match), 12)
