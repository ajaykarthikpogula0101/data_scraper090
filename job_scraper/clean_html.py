"""Convert archived job-description HTML to readable plain text."""

import html
import re

from bs4 import BeautifulSoup


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_plain_text(value):
    """Decode entities, remove markup, and retain useful block line breaks."""
    if value is None:
        return ""
    decoded = html.unescape(str(value).replace("\x00", ""))
    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    # Handles escaped/doubly encoded tag fragments without leaving markup behind.
    result = _TAG_RE.sub("", result)
    return result.strip()
