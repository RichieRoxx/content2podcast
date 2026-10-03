"""XML text helpers shared by the SSML and RSS generators."""

from __future__ import annotations

import re

# Everything outside the XML 1.0 ``Char`` production (tab, LF, CR, and the allowed ranges).
_INVALID_XML_CHARS = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def strip_invalid_xml_chars(text: str) -> str:
    """Remove characters that are not allowed in XML 1.0 documents (control characters, lone
    surrogates, ``U+FFFE`` / ``U+FFFF``)."""
    return _INVALID_XML_CHARS.sub("", text)
