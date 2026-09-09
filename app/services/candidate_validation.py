"""Supported-format validation for generated candidates.

V1 ships only flat-file digital products. Anything that requires software,
ongoing delivery, or media production is rejected here regardless of what a
provider proposes.
"""

import re
from typing import Any

from app.domain.enums import ProductFormat

# Free-text spellings a provider might use for supported formats.
_FORMAT_ALIASES: dict[str, ProductFormat] = {
    "pdf": ProductFormat.PDF_GUIDE,
    "pdf guide": ProductFormat.PDF_GUIDE,
    "pdf_guide": ProductFormat.PDF_GUIDE,
    "guide": ProductFormat.PDF_GUIDE,
    "ebook": ProductFormat.PDF_GUIDE,
    "workbook": ProductFormat.WORKBOOK,
    "checklist": ProductFormat.CHECKLIST,
    "checklists": ProductFormat.CHECKLIST,
    "template pack": ProductFormat.TEMPLATE_PACK,
    "template_pack": ProductFormat.TEMPLATE_PACK,
    "templates": ProductFormat.TEMPLATE_PACK,
    "template": ProductFormat.TEMPLATE_PACK,
    "spreadsheet": ProductFormat.SPREADSHEET_TOOL,
    "spreadsheet tool": ProductFormat.SPREADSHEET_TOOL,
    "spreadsheet_tool": ProductFormat.SPREADSHEET_TOOL,
    "xlsx": ProductFormat.SPREADSHEET_TOOL,
    "xlsx tool": ProductFormat.SPREADSHEET_TOOL,
    "excel": ProductFormat.SPREADSHEET_TOOL,
    "csv": ProductFormat.DATA_TEMPLATE,
    "csv template": ProductFormat.DATA_TEMPLATE,
    "data template": ProductFormat.DATA_TEMPLATE,
    "data_template": ProductFormat.DATA_TEMPLATE,
    "printable": ProductFormat.PRINTABLE_BUNDLE,
    "printables": ProductFormat.PRINTABLE_BUNDLE,
    "printable bundle": ProductFormat.PRINTABLE_BUNDLE,
    "printable_bundle": ProductFormat.PRINTABLE_BUNDLE,
    "reference bundle": ProductFormat.PRINTABLE_BUNDLE,
}

# Signals of product types V1 explicitly does not build.
_UNSUPPORTED_TERMS = [
    "saas",
    "software",
    "micro-app",
    "micro app",
    "microapp",
    "web app",
    "mobile app",
    "application",
    "app",
    "plugin",
    "extension",
    "coaching",
    "community",
    "membership",
    "mastermind",
    "cohort",
    "course",
    "bootcamp",
    "webinar",
    "video",
    "subscription",
]

_UNSUPPORTED_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _UNSUPPORTED_TERMS) + r")\b"
)


def resolve_format(raw_format: Any) -> ProductFormat | None:
    """Map a provider's proposed format to a supported ProductFormat, or None."""
    if isinstance(raw_format, ProductFormat):
        return raw_format
    if not isinstance(raw_format, str):
        return None
    key = raw_format.strip().lower().replace("-", " ")
    try:
        return ProductFormat(raw_format.strip().upper().replace(" ", "_").replace("-", "_"))
    except ValueError:
        return _FORMAT_ALIASES.get(key)


def unsupported_signal(*texts: str) -> str | None:
    """Return the first unsupported-product term found in the given texts."""
    for text in texts:
        match = _UNSUPPORTED_RE.search(text.lower())
        if match:
            return match.group(1)
    return None


def validate_candidate_format(
    raw: dict[str, Any],
    seed_keyword: str = "",
) -> tuple[ProductFormat | None, str | None]:
    """Validate a raw candidate dict against V1 format rules.

    Returns (resolved_format, rejection_reason). Exactly one of the two is None.
    The seed keyword's own words are exempt from the unsupported-term scan so a
    niche like "video editing" is not rejected for containing "video".
    """
    proposed = raw.get("proposed_format")
    resolved = resolve_format(proposed)
    if resolved is None:
        return None, f"unsupported_format: {proposed!r} is not a V1-supported product format"

    title = str(raw.get("title", "")).lower()
    if seed_keyword:
        title = title.replace(seed_keyword.strip().lower(), " ")

    signal = unsupported_signal(str(raw.get("proposed_format", "")), title)
    if signal:
        return None, f"unsupported_format_signal: candidate references '{signal}', which V1 does not build"

    return resolved, None
