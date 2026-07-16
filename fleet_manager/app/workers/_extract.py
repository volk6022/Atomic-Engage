"""Contact-extraction sweep over free text (research-agent enrichment, §3.2/§3.3).

Pulls URLs, e-mails and phone numbers out of a chat About / bio / pinned message /
post text so the org-card `contacts.*` fields can be filled without joining anything.
Pure regex, no I/O — deliberately conservative (precision over recall): the LLM
downstream disambiguates.
"""
from __future__ import annotations

import re
from typing import Optional

_URL_RE = re.compile(
    r"\b(?:https?://|t\.me/|www\.)[^\s<>()\"']+|"
    r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.(?:ru|com|org|net|io|me|dev|app)\b(?:/[^\s<>()\"']*)?",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# International-ish phone: optional +, 10–15 digits allowing spaces/dashes/parens.
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,16}\d(?![\w])")


def _dedup(seq) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        x = x.strip().rstrip(".,;)")
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def extract_contacts(*texts: Optional[str]) -> dict:
    """Sweep one or more text blobs; return {urls, emails, phones} (deduped)."""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return {"urls": [], "emails": [], "phones": []}

    emails = _EMAIL_RE.findall(blob)
    # Don't double-count the domain inside an e-mail as a URL.
    blob_no_emails = _EMAIL_RE.sub(" ", blob)
    urls = _URL_RE.findall(blob_no_emails)
    phones = [p for p in _PHONE_RE.findall(blob) if sum(c.isdigit() for c in p) >= 10]

    return {
        "urls": _dedup(urls),
        "emails": _dedup(emails),
        "phones": _dedup(phones),
    }
