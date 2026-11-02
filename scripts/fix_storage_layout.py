"""Move existing files into the standard storage layout and update the database. No AI call.

    storage/
      standards/<family>/part1|part2|other/<name>_<edition>.pdf     (standalone: standards/<family>/...)
      reports/<report name>/<report name>.pdf
      reports/<report name>/outputs/<analysis_id>.md
      reports/<report name>/outputs/evaluation_<id>/Technical_Evaluation_<no>.pdf | .xlsx | .json

Fixes, for files saved before this layout:
  - a second root folder from an old prefix (standards_pdf/, reports_pdf/)  -> standards/, reports/
  - a standard named after its long filename ('IEC_60065-_AUDIO_VIDEO_...') -> identified as 'IEC 60065'
  - report folders named by id (reports/<uuid>/)                           -> reports/<report name>/

Run from the project folder (same .env as the server; stop the server first):
    python scripts/fix_storage_layout.py --dry-run      # show what would move
    python scripts/fix_storage_layout.py                # move files + update the database

Works for local storage and S3 (files are copied to the new key, then the old key is deleted).
Set STANDARDS_PREFIX=standards/ and REPORTS_PREFIX=reports/ in .env before running.
"""
from __future__ import annotations

import argparse
import asyncio
import posixpath
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.db.database import SessionLocal, engine, init_db  # noqa: E402
from app.models import Evaluation, Report, ReportFile, Standard  # noqa: E402
from app.service.standard_service import PART_DIRS, _identify  # noqa: E402
from app.service.storage_service import get_storage  # noqa: E402
from app.utils.pdf_utils import detect_standards, extract_text, parse_standard_name, safe_filename, standard_name_key  # noqa: E402


def _standard_key(prefix: str, family: str, part_type: str, name: str, edition: str | None) -> str:
    folder = prefix + safe_filename(family) + "/"
    if part_type in PART_DIRS:
        folder += PART_DIRS[part_type] + "/"
    stem = safe_filename(name) + (f"_{safe_filename(edition)}" if edition else "")
    return f"{folder}{stem}.pdf"


async def _move(storage, old: str, new: str, dry: bool, moved: dict[str, str]) -> str:
    if old == new or old in moved:
        return moved.get(old, new)
    print(f"  move {old}\n    -> {new}")
    if not dry:
        if await storage.exists(old):
            await storage.put(new, await storage.get(old),
                              content_type="application/pdf" if new.endswith(".pdf") else "application/octet-stream")
            await storage.delete(old)
        else:
            print("    (source file missing - only the database path is updated)")
    moved[old] = new
    return new


async def main(dry: bool) -> None:
    s = get_settings()
    storage = get_storage()
    await init_db()
    moved: dict[str, str] = {}
    async with SessionLocal() as db:
        # ---------------------------------------------------------- standards
        print(f"Standards -> {s.standards_prefix}<family>/<part>/")
        used: set[str] = set()
        for std in (await db.scalars(select(Standard))).all():
            ident = parse_standard_name(std.name)
            try:                                              # identify again from the PDF text
                data = await storage.get(std.storage_key)
                detected = detect_standards(extract_text(data, 3))
            except Exception as exc:
                print(f"  ! cannot read {std.storage_key}: {exc} (filename used)")
                detected = []
            name, edition = _identify(detected, std.original_filename or std.name, None)
            edition = edition or std.edition
            if name != std.name or edition != std.edition:
                ident = parse_standard_name(name)
                print(f"  rename '{std.name}' -> '{name}'" + (f" edition {edition}" if edition else "")
                      + f" ({ident.part_type})")
                if not dry:
                    std.name, std.name_key, std.edition = name, standard_name_key(name), edition
                    std.family, std.family_key = ident.family, standard_name_key(ident.family)
                    std.part_type, std.part_number = ident.part_type, ident.part_number
            key = _standard_key(s.standards_prefix, ident.family, ident.part_type, name, edition)
            if key in used:                                   # same name + edition, different file
                key = key[:-4] + f"_{std.sha256[:8]}.pdf"
            used.add(key)
            std.storage_key = await _move(storage, std.storage_key, key, dry, moved)

        # Part 2 -> Part 1 links (names may have changed above)
        await db.flush()
        stds = (await db.scalars(select(Standard))).all()
        for child in (c for c in stds if c.part_type == "part2"):
            part1s = [p for p in stds if p.part_type == "part1" and p.family_key == child.family_key]
            best = next((p for p in part1s if child.parent_edition_hint and p.edition == child.parent_edition_hint),
                        part1s[0] if part1s else None)
            if best and child.parent_id != best.id:
                print(f"  link {child.name} -> {best.name}")
                child.parent_id = best.id

        # ---------------------------------------------------------- reports
        print(f"Reports -> {s.reports_prefix}<report name>/")
        folders: set[str] = set()
        for rf in (await db.scalars(select(ReportFile))).all():
            stem = safe_filename((rf.original_filename or "report.pdf").rsplit(".", 1)[0])
            folder = f"{s.reports_prefix}{stem}/"
            if folder in folders:
                folder = f"{s.reports_prefix}{stem}_{rf.id[:8]}/"
            folders.add(folder)
            old_folder = rf.storage_key.rsplit("/", 1)[0] + "/"
            if old_folder == folder:
                continue
            rf.storage_key = await _move(storage, rf.storage_key, f"{folder}{stem}.pdf", dry, moved)

            def _new(path: str | None) -> str | None:
                if not path:
                    return path
                return folder + path[len(old_folder):] if path.startswith(old_folder) else \
                    folder + "outputs/" + posixpath.basename(path)

            for row in (await db.scalars(select(Report).where(Report.report_file_id == rf.id))).all():
                if row.output_path:
                    row.output_path = await _move(storage, row.output_path, _new(row.output_path), dry, moved)
            for ev in (await db.scalars(select(Evaluation).where(Evaluation.report_file_id == rf.id))).all():
                for attr in ("pdf_path", "xlsx_path"):
                    path = getattr(ev, attr)
                    if path:
                        setattr(ev, attr, await _move(storage, path, _new(path), dry, moved))
                if ev.pdf_path:
                    old_json = next((o for o, n in moved.items() if n == ev.pdf_path), None)
                    if old_json and await storage.exists(old_json[:-4] + ".json"):
                        await _move(storage, old_json[:-4] + ".json", ev.pdf_path[:-4] + ".json", dry, moved)

        if dry:
            await db.rollback()
            print(f"\nDry run: {len(moved)} file(s) would move. Nothing changed.")
        else:
            await db.commit()
            print(f"\nDone: {len(moved)} file(s) moved, database updated.")
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only show what would move")
    asyncio.run(main(ap.parse_args().dry_run))