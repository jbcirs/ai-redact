"""Shared pieces of the handler contract (see docs/plans/handler-spec.md)."""


class UnsupportedFormatError(Exception):
    """Raised when a file cannot be processed. The message is shown to the
    user and must say WHAT is unsupported and what to do instead."""


def redaction_text(match: str) -> str:
    """Replacement string for a redacted span in text-based formats."""
    return "█" * min(len(match), 12)
