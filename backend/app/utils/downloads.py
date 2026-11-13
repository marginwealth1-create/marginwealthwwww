"""The Content-Disposition header of a file download."""

from __future__ import annotations

import unicodedata
from email.utils import encode_rfc2231


def attachment(filename: str) -> str:
    """Starlette sends headers as latin-1, so a client named in Devanagari
    made the download a 500 and a quote in a name broke the header.
    filename= carries an ASCII copy; when it isn't the name, filename*
    (RFC 5987) carries the real one."""
    plain = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    plain = "".join(c for c in plain if c.isprintable() and c not in '"\\')
    if plain == filename:
        return f'attachment; filename="{filename}"'
    return f'attachment; filename="{plain}"; filename*={encode_rfc2231(filename, "utf-8")}'
