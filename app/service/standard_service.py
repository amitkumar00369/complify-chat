"""Standards: local storage in a family/part folder tree + Part 1 <-> Part 2 linking.

storage/standards/
└── IEC_60335/
    ├── part1/IEC_60335-1_2010.pdf
    └── part2/IEC_60335-2-7_2012.pdf      (parent_id -> IEC 60335-1:2010)
"""
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import Standard
from app.service.ai_service import AIService
from app.service.storage_service import StorageBackend
from app.utils.exceptions import NotFoundError
from app.utils.pdf_utils import (
    detect_standards, extract_text, parse_standard_name, safe_filename, sha256_hex,
    standard_name_key,
)

logger = logging.getLogger(__name__)

PART_DIRS = {"part1": "part1", "part2": "part2", "part": "other"}


class StandardService:
    def __init__(self, db: Session, storage: StorageBackend):
        self.db = db
        self.storage = storage
        self.settings = get_settings()

    # ------------------------------------------------------------------ upload
    def register(self, data: bytes, filename: str, name: str | None = None,
                 edition: str | None = None) -> tuple[Standard, bool, list[str]]:
        """Save PDF + insert row + link parent/children.
        Returns (standard, created, warnings). Same file twice -> existing row."""
        sha = sha256_hex(data)
        existing = self.db.scalar(select(Standard).where(Standard.sha256 == sha))
        if existing:
            return existing, False, []

        detected = detect_standards(extract_text(data, max_pages=3))
        if not name:
            if detected:
                name = detected[0].name
                edition = edition or detected[0].edition
            else:
                name = Path(filename).stem
        ident = parse_standard_name(name)

        # Part 2 PDFs say which Part 1 edition they go with: "use with IEC 60335-1:2010"
        hint = None
        if ident.part_type == "part2":
            fam_key = standard_name_key(ident.family)
            for d in detected:
                p = parse_standard_name(d.name)
                if p.part_type == "part1" and standard_name_key(p.family) == fam_key and d.edition:
                    hint = d.edition
                    break

        key = self._storage_key(ident, edition, sha)
        self.storage.put(key, data, metadata={"original_filename": filename, "standard": ident.name})

        std = Standard(
            name=ident.name, name_key=standard_name_key(ident.name), edition=edition,
            family=ident.family, family_key=standard_name_key(ident.family),
            part_type=ident.part_type, part_number=ident.part_number,
            parent_edition_hint=hint, original_filename=filename, storage_key=key,
            sha256=sha, size_bytes=len(data),
        )
        self.db.add(std)
        self.db.flush()

        warnings: list[str] = []
        if std.part_type == "part2":
            warnings += self._link_parent(std)
        elif std.part_type == "part1":
            self._adopt_children(std)
        self.db.commit()
        logger.info("Registered standard %s:%s (%s) at %s", std.name, std.edition, std.part_type, key)
        return std, True, warnings

    def _storage_key(self, ident, edition: str | None, sha: str) -> str:
        folder = self.settings.standards_prefix + safe_filename(ident.family) + "/"
        if ident.part_type in PART_DIRS:
            folder += PART_DIRS[ident.part_type] + "/"
        stem = safe_filename(ident.name) + (f"_{safe_filename(edition)}" if edition else "")
        key = f"{folder}{stem}.pdf"
        taken = self.db.scalar(select(Standard.id).where(Standard.storage_key == key))
        if taken or self.storage.exists(key):           # same name+edition, different file
            key = f"{folder}{stem}_{sha[:8]}.pdf"
        return key

    # ------------------------------------------------------- parent / children
    def _best_parent(self, child: Standard) -> Standard | None:
        stmt = (select(Standard)
                .where(Standard.family_key == child.family_key, Standard.part_type == "part1")
                .order_by(Standard.created_at.desc()))
        part1s = list(self.db.scalars(stmt))
        if not part1s:
            return None
        if child.parent_edition_hint:
            for p in part1s:
                if p.edition == child.parent_edition_hint:
                    return p
        return part1s[0]  # newest Part 1 as fallback

    def _link_parent(self, child: Standard) -> list[str]:
        parent = self._best_parent(child)
        child.parent_id = parent.id if parent else None
        if not parent:
            return [f"Parent Part 1 for {child.name} ({child.family}-1"
                    f"{':' + child.parent_edition_hint if child.parent_edition_hint else ''}) "
                    "is not uploaded yet. It will be linked automatically when uploaded."]
        if child.parent_edition_hint and parent.edition != child.parent_edition_hint:
            return [f"{child.name} expects {child.family}-1:{child.parent_edition_hint}, "
                    f"linked to edition {parent.edition or 'unknown'} for now."]
        return []

    def _adopt_children(self, part1: Standard) -> None:
        """New Part 1 uploaded -> (re)link Part 2 files of the same family."""
        stmt = select(Standard).where(Standard.family_key == part1.family_key,
                                      Standard.part_type == "part2")
        for child in self.db.scalars(stmt):
            best = self._best_parent(child)
            if best and child.parent_id != best.id:
                child.parent_id = best.id

    def children(self, std: Standard) -> list[Standard]:
        return list(self.db.scalars(select(Standard).where(Standard.parent_id == std.id)))

    def parent(self, std: Standard) -> Standard | None:
        return self.db.get(Standard, std.parent_id) if std.parent_id else None

    def with_parents(self, stds: list[Standard]) -> list[Standard]:
        """[IEC 60335-2-7] -> [IEC 60335-1, IEC 60335-2-7] (parent first, no duplicates)."""
        out: list[Standard] = []
        for s in stds:
            p = self.parent(s) if s.part_type == "part2" else None
            for item in (p, s):
                if item and item.id not in {o.id for o in out}:
                    out.append(item)
        return out

    # --------------------------------------------------------------- queries
    def get(self, standard_id: str) -> Standard:
        std = self.db.get(Standard, standard_id)
        if not std:
            raise NotFoundError(f"Standard '{standard_id}' not found")
        return std

    def get_many(self, ids: list[str], *, ignore_missing: bool = False) -> list[Standard]:
        result = []
        for sid in ids:
            std = self.db.get(Standard, sid)
            if std:
                result.append(std)
            elif not ignore_missing:
                raise NotFoundError(f"Standard '{sid}' not found")
        return result

    def list_all(self, limit: int = 100, offset: int = 0, family: str | None = None) -> "list[Standard]":
        stmt = select(Standard)
        if family:
            stmt = stmt.where(Standard.family_key == standard_name_key(family))
        stmt = stmt.order_by(Standard.family, Standard.part_type, Standard.name).limit(limit).offset(offset)
        return list(self.db.scalars(stmt))

    def find_ref(self, name: str, edition: str | None) -> Standard | None:
        """Exact edition if uploaded, otherwise the newest file with that name."""
        stmt = (select(Standard).where(Standard.name_key == standard_name_key(name))
                .order_by(Standard.created_at.desc()))
        rows = list(self.db.scalars(stmt))
        if not rows:
            return None
        if edition:
            for r in rows:
                if r.edition == edition:
                    return r
        return rows[0]

    def pick_for_report(self, refs: list[tuple[str, str | None]]) -> list[Standard]:
        """Standards a report needs: first Part 2 found + its Part 1.
        If the report names a Part 1 edition itself, that one wins over the linked parent."""
        found: list[Standard] = []
        for name, edition in refs:
            s = self.find_ref(name, edition)
            if s and s.id not in {f.id for f in found}:
                found.append(s)
        part2 = next((s for s in found if s.part_type == "part2"), None)
        if part2:
            p1 = next((s for s in found if s.part_type == "part1"
                       and s.family_key == part2.family_key), None) or self.parent(part2)
            return [x for x in (p1, part2) if x]
        return found[: self.settings.max_standards_per_request]

    def ensure_openai_file(self, std: Standard, ai: AIService) -> str:
        """Upload the PDF to OpenAI once and remember the file id."""
        if std.openai_file_id:
            return std.openai_file_id
        data = self.storage.get(std.storage_key)
        std.openai_file_id = ai.upload_file(std.original_filename, data)
        self.db.commit()
        return std.openai_file_id
