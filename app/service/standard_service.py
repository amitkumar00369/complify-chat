"""Standards: local storage in a family/part folder tree + Part 1 <-> Part 2 linking.

storage/standards/
└── IEC_60335/
    ├── part1/IEC_60335-1_2010.pdf
    └── part2/IEC_60335-2-7_2012.pdf      (parent_id -> IEC 60335-1:2010)
"""
import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models import Standard
from app.service.ai_service import AIService
from app.service.storage_service import StorageBackend
from app.utils.exceptions import NotFoundError, StorageError
from app.utils.pdf_utils import (
    detect_standards, extract_text, parse_standard_name, safe_filename, sha256_hex,
    standard_name_key,
)

logger = logging.getLogger(__name__)

PART_DIRS = {"part1": "part1", "part2": "part2", "part": "other"}


class StandardService:
    def __init__(self, db: AsyncSession, storage: StorageBackend):
        self.db = db
        self.storage = storage
        self.settings = get_settings()

    # ------------------------------------------------------------------ upload
    async def register(self, data: bytes, filename: str, name: str | None = None,
                 edition: str | None = None) -> tuple[Standard, bool, list[str]]:
        """Save PDF + insert row + link parent/children.
        Returns (standard, created, warnings). Same file twice -> existing row."""
        sha = sha256_hex(data)
        existing = await self.db.scalar(select(Standard).where(Standard.sha256 == sha))
        if existing:
            if not await self.storage.exists(existing.storage_key):      # DB row survived, file was deleted
                logger.warning("Standard file missing on disk, restoring %s", existing.storage_key)
                await self.storage.put(existing.storage_key, data,
                                 metadata={"original_filename": filename, "standard": existing.name})
            return existing, False, []

        detected = detect_standards(await asyncio.to_thread(extract_text, data, 3))
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

        key = await self._storage_key(ident, edition, sha)
        await self.storage.put(key, data, metadata={"original_filename": filename, "standard": ident.name})

        std = Standard(
            name=ident.name, name_key=standard_name_key(ident.name), edition=edition,
            family=ident.family, family_key=standard_name_key(ident.family),
            part_type=ident.part_type, part_number=ident.part_number,
            parent_edition_hint=hint, original_filename=filename, storage_key=key,
            sha256=sha, size_bytes=len(data),
        )
        self.db.add(std)
        await self.db.flush()

        warnings: list[str] = []
        if std.part_type == "part2":
            warnings += await self._link_parent(std)
        elif std.part_type == "part1":
            await self._adopt_children(std)
        await self.db.commit()
        logger.info("Registered standard %s:%s (%s) at %s", std.name, std.edition, std.part_type, key)
        return std, True, warnings

    async def _storage_key(self, ident, edition: str | None, sha: str) -> str:
        folder = self.settings.standards_prefix + safe_filename(ident.family) + "/"
        if ident.part_type in PART_DIRS:
            folder += PART_DIRS[ident.part_type] + "/"
        stem = safe_filename(ident.name) + (f"_{safe_filename(edition)}" if edition else "")
        key = f"{folder}{stem}.pdf"
        taken = await self.db.scalar(select(Standard.id).where(Standard.storage_key == key))
        if taken or await self.storage.exists(key):           # same name+edition, different file
            key = f"{folder}{stem}_{sha[:8]}.pdf"
        return key

    # ------------------------------------------------------- parent / children
    async def _best_parent(self, child: Standard) -> Standard | None:
        stmt = (select(Standard)
                .where(Standard.family_key == child.family_key, Standard.part_type == "part1")
                .order_by(Standard.created_at.desc()))
        part1s = list(await self.db.scalars(stmt))
        if not part1s:
            return None
        if child.parent_edition_hint:
            for p in part1s:
                if p.edition == child.parent_edition_hint:
                    return p
        return part1s[0]  # newest Part 1 as fallback

    async def _link_parent(self, child: Standard) -> list[str]:
        parent = await self._best_parent(child)
        child.parent_id = parent.id if parent else None
        if not parent:
            return [f"Parent Part 1 for {child.name} ({child.family}-1"
                    f"{':' + child.parent_edition_hint if child.parent_edition_hint else ''}) "
                    "is not uploaded yet. It will be linked automatically when uploaded."]
        if child.parent_edition_hint and parent.edition != child.parent_edition_hint:
            return [f"{child.name} expects {child.family}-1:{child.parent_edition_hint}, "
                    f"linked to edition {parent.edition or 'unknown'} for now."]
        return []

    async def _adopt_children(self, part1: Standard) -> None:
        """New Part 1 uploaded -> (re)link Part 2 files of the same family."""
        stmt = select(Standard).where(Standard.family_key == part1.family_key,
                                      Standard.part_type == "part2")
        for child in await self.db.scalars(stmt):
            best = await self._best_parent(child)
            if best and child.parent_id != best.id:
                child.parent_id = best.id

    async def children(self, std: Standard) -> list[Standard]:
        return list(await self.db.scalars(select(Standard).where(Standard.parent_id == std.id)))

    async def parent(self, std: Standard) -> Standard | None:
        return await self.db.get(Standard, std.parent_id) if std.parent_id else None

    async def with_parents(self, stds: list[Standard]) -> list[Standard]:
        """[IEC 60335-2-7] -> [IEC 60335-1, IEC 60335-2-7] (parent first, no duplicates)."""
        out: list[Standard] = []
        for s in stds:
            p = await self.parent(s) if s.part_type == "part2" else None
            for item in (p, s):
                if item and item.id not in {o.id for o in out}:
                    out.append(item)
        return out

    # --------------------------------------------------------------- queries
    async def get(self, standard_id: str) -> Standard:
        std = await self.db.get(Standard, standard_id)
        if not std:
            raise NotFoundError(f"Standard '{standard_id}' not found")
        return std

    async def get_many(self, ids: list[str], *, ignore_missing: bool = False) -> list[Standard]:
        result = []
        for sid in ids:
            std = await self.db.get(Standard, sid)
            if std:
                result.append(std)
            elif not ignore_missing:
                raise NotFoundError(f"Standard '{sid}' not found")
        return result

    async def list_all(self, limit: int = 100, offset: int = 0, family: str | None = None) -> "list[Standard]":
        stmt = select(Standard)
        if family:
            stmt = stmt.where(Standard.family_key == standard_name_key(family))
        stmt = stmt.order_by(Standard.family, Standard.part_type, Standard.name).limit(limit).offset(offset)
        return list(await self.db.scalars(stmt))

    async def find_ref(self, name: str, edition: str | None) -> Standard | None:
        """Exact edition if uploaded, otherwise the newest file with that name."""
        stmt = (select(Standard).where(Standard.name_key == standard_name_key(name))
                .order_by(Standard.created_at.desc()))
        rows = list(await self.db.scalars(stmt))
        if not rows:
            return None
        if edition:
            for r in rows:
                if r.edition == edition:
                    return r
        return rows[0]

    async def pick_for_report(self, refs: list[tuple[str, str | None]]) -> list[Standard]:
        """Standards a report needs: first Part 2 found + its Part 1.
        If the report names a Part 1 edition itself, that one wins over the linked parent."""
        found: list[Standard] = []
        for name, edition in refs:
            s = await self.find_ref(name, edition)
            if s and s.id not in {f.id for f in found}:
                found.append(s)
        part2 = next((s for s in found if s.part_type == "part2"), None)
        if part2:
            p1 = next((s for s in found if s.part_type == "part1"
                       and s.family_key == part2.family_key), None) or await self.parent(part2)
            return [x for x in (p1, part2) if x]
        return found[: self.settings.max_standards_per_request]

    async def ensure_openai_file(self, std: Standard, ai: AIService) -> str:
        """Upload the PDF to OpenAI once and remember the file id."""
        if std.openai_file_id:
            return std.openai_file_id
        if not await self.storage.exists(std.storage_key):
            raise StorageError(f"Standard PDF {std.name} is missing on disk ({std.storage_key}). "
                               "Upload the same standard again via POST /standards to restore it.")
        data = await self.storage.get(std.storage_key)
        std.openai_file_id = await ai.upload_file(std.original_filename, data)
        await self.db.commit()
        return std.openai_file_id
