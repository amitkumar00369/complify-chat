import re
import unicodedata
from difflib import SequenceMatcher


def normalize_prompt(text: str) -> str:
    """Lower-case, collapse whitespace, drop trailing punctuation.

    "  Evaluate   this report!! " and "evaluate this report" -> same key.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(" ?.!,;:")


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def standards_key(standard_ids: list[str]) -> str:
    return ",".join(sorted(standard_ids))


def make_title(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
