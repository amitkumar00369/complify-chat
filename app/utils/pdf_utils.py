"""PDF helpers: upload validation, hashing, text extraction, standard detection."""
import hashlib
import io
import logging
import re
from dataclasses import dataclass

from fastapi import UploadFile
from pypdf import PdfReader

from app.utils.exceptions import BadRequestError, PayloadTooLargeError, UnsupportedFileError

logger = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)  # broken xref warnings flood the console

PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class UploadedPdf:
    filename: str
    data: bytes


@dataclass(frozen=True)
class DetectedStandard:
    name: str
    edition: str | None = None


def read_pdf_upload(upload: UploadFile, max_bytes: int) -> UploadedPdf:
    """Read an UploadFile fully, enforcing size + PDF signature."""
    filename = (upload.filename or "file.pdf").replace("\\", "/").split("/")[-1]
    data = upload.file.read(max_bytes + 1)
    if not data:
        raise BadRequestError(f"'{filename}' is empty")
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"'{filename}' is larger than {max_bytes // (1024 * 1024)} MB"
        )
    if PDF_MAGIC not in data[:1024]:
        raise UnsupportedFileError(f"'{filename}' is not a PDF")
    return UploadedPdf(filename=filename, data=data)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_text(data: bytes, max_pages: int = 5) -> str:
    """Best-effort text of the first `max_pages` pages ('' for scanned/broken PDFs)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as exc:  # pypdf raises many types on malformed files
        logger.warning("PDF text extraction failed: %s", exc)
        return ""


# Upper-case prefixes only, so normal words like "is 10" don't match.
_STANDARD_RE = re.compile(
    r"\b(BS\s+EN|AS/NZS|IEC|EN|UL|ISO|ANSI|CSA|IS)\s*[:\-]?\s*"
    r"(\d{2,6}(?:\s*[-–]\s*\d{1,3}){0,3})(?!\w)"   # skip 'EN201222034S', 'IEC 60335_2_41M'
    r"(?:\s*:\s*((?:19|20)\d{2}))?"
)


def standard_name_key(name: str) -> str:
    """'IEC 60335-2-7' -> 'IEC6033527' (used for matching)."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


@dataclass(frozen=True)
class StandardIdentity:
    name: str                 # "IEC 60335-2-7"
    family: str               # "IEC 60335"
    part_type: str            # part1 | part2 | part | standalone
    part_number: str | None   # "1", "2-7", "3-2", None


_NAME_RE = re.compile(r"^(?P<prefix>.*?)\s*(?P<base>\d{2,6})(?P<parts>(?:-\d{1,3})*)$")


def parse_standard_name(name: str) -> StandardIdentity:
    """'IEC 60335-2-7' -> family 'IEC 60335', part2, '2-7'.

    part1      = '-1'            (general requirements, the parent)
    part2      = '-2-x'          (particular requirements, child of part1)
    part       = any other part  (e.g. IEC 61000-3-2, no parent linking)
    standalone = no part number  (e.g. UL 2157)
    """
    name = re.sub(r"\s+", " ", name.strip())
    m = _NAME_RE.match(name)
    if not m:
        return StandardIdentity(name, name, "standalone", None)
    prefix, base = m.group("prefix").strip(), m.group("base")
    parts = [p for p in m.group("parts").split("-") if p]
    family = f"{prefix} {base}".strip()
    if not parts:
        return StandardIdentity(name, family, "standalone", None)
    number = "-".join(parts)
    if parts == ["1"]:
        return StandardIdentity(name, family, "part1", number)
    if parts[0] == "2" and len(parts) >= 2:
        return StandardIdentity(name, family, "part2", number)
    return StandardIdentity(name, family, "part", number)


def safe_filename(text: str) -> str:
    """'IEC 60335-2-7' -> 'IEC_60335-2-7' (safe for any OS)."""
    return re.sub(r"[^A-Za-z0-9.\-]+", "_", text).strip("_") or "file"


def format_ref(name: str, edition: str | None) -> str:
    return f"{name}:{edition}" if edition else name


def parse_ref(ref: str) -> tuple[str, str | None]:
    name, _, edition = ref.partition(":")
    return name.strip(), (edition.strip() or None)


def detect_standards(text: str) -> list[DetectedStandard]:
    """Find standard references (IEC 60335-1:2020, EN 60335-2-7 ...) in order of appearance."""
    seen: dict[str, DetectedStandard] = {}
    for m in _STANDARD_RE.finditer(text):
        prefix = re.sub(r"\s+", " ", m.group(1))
        number = re.sub(r"\s+", "", m.group(2)).replace("–", "-")
        name = f"{prefix} {number}"
        key = standard_name_key(name)
        if key not in seen:
            seen[key] = DetectedStandard(name=name, edition=m.group(3))
        elif m.group(3) and not seen[key].edition:
            seen[key] = DetectedStandard(name=name, edition=m.group(3))
    return list(seen.values())
