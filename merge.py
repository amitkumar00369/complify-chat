"""Merge another Complify data set (SQLite DB + storage folder) into this one, adding only what is missing.

Why: reports that were already evaluated somewhere else (another laptop, an older copy) keep their stored
answers, so sending the same report again returns the stored evaluation instead of a new paid model call.

    python scripts/merge_data.py --source-db  ../old/data/complify.db --source-storage ../old/storage --dry-run
    python scripts/merge_data.py --source-db  ../old/data/complify.db --source-storage ../old/storage

Defaults for the target: data/complify.db and storage/ of this project (stop the server first).
The target DB is backed up first (data/complify.db.bak-<time>); the source is never changed.

Rules
- report_files / standards: the SAME file (same sha256) already in the target -> the target row is used
  and every reference to the source row is re-pointed to it; a new file -> row + PDF are copied.
- reports (chat answers .md) and evaluations (PDF / XLSX / JSON): copied when their id is missing; their
  files are copied into the target's folder of that report (reports/<report name>/outputs/...).
- standards_key of copied answers is rebuilt the way the current code selects standards (a Part 2
  always brings its Part 1), so the stored answer is found and reused for the same report.
- chat_sessions / chat_messages: copied when missing (answers belong to a chat).
- Nothing in the target is overwritten or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- helpers
def open_source(db: Path) -> sqlite3.Connection:
    """Copy the source DB (+ -wal/-shm) to a temp dir and fold the WAL in: the original stays untouched."""
    tmp = Path(tempfile.mkdtemp(prefix="complify-merge-"))
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.is_file():
            shutil.copy2(p, tmp / ("src.db" + suffix))
    con = sqlite3.connect(tmp / "src.db")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.row_factory = sqlite3.Row
    return con


def cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def rows(con: sqlite3.Connection, table: str) -> list[dict]:
    if not cols(con, table):
        return []
    return [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]


def jload(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def report_dir(storage_key: str) -> str:
    """'reports/x/x.pdf' -> 'reports/x/' (same as report_service.report_dir)."""
    return storage_key.rsplit("/", 1)[0] + "/"


class Merger:
    def __init__(self, src: sqlite3.Connection, src_storage: Path, dst: sqlite3.Connection,
                 dst_storage: Path, dry: bool):
        self.src, self.dst = src, dst
        self.src_storage, self.dst_storage = src_storage, dst_storage
        self.dry = dry
        self.map_rf: dict[str, str] = {}       # source report_file id -> target id
        self.map_std: dict[str, str] = {}      # source standard id   -> target id
        self.folder: dict[str, str] = {}       # source report folder 'reports/a/' -> target folder
        self.copied_files = 0
        self.stats: dict[str, dict[str, int]] = {}
        self._src_by_sha: dict[str, Path] | None = None
        self._planned: set[str] = set()

    # ---------------------------------------------------------------- files
    def _find_src_file(self, key: str, sha: str | None = None) -> Path | None:
        p = self.src_storage / key
        if p.is_file():
            return p
        if sha:                       # row points to an old path: find the file by its content
            if self._src_by_sha is None:
                self._src_by_sha = {sha256_file(f): f for f in self.src_storage.rglob("*.pdf") if f.is_file()}
            return self._src_by_sha.get(sha)
        return None

    def _copy(self, src_file: Path | None, dst_key: str) -> bool:
        if src_file is None:
            print(f"    ! source file missing for {dst_key}")
            return False
        target = self.dst_storage / dst_key
        if target.is_file() or dst_key in self._planned:
            return True
        self._planned.add(dst_key)
        print(f"    + file {dst_key}")
        self.copied_files += 1
        if not self.dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target)
        return True

    def _free_key(self, key: str, rid: str) -> str:
        """Target already has another file at this path -> add the id."""
        if not (self.dst_storage / key).exists():
            return key
        stem, ext = key.rsplit(".", 1)
        return f"{stem}_{rid[:8]}.{ext}"

    # ---------------------------------------------------------------- rows
    def _insert(self, table: str, row: dict) -> None:
        names = [c for c in cols(self.dst, table) if c in row]
        vals = [json.dumps(row[c]) if isinstance(row[c], (list, dict)) else row[c] for c in names]
        if not self.dry:
            self.dst.execute(f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" * len(names))})', vals)

    def _count(self, table: str, what: str) -> None:
        self.stats.setdefault(table, {}).setdefault(what, 0)
        self.stats[table][what] += 1

    def _ids(self, table: str) -> set[str]:
        return {r[0] for r in self.dst.execute(f'SELECT id FROM "{table}"')}

    def _by_sha(self, table: str) -> dict[str, dict]:
        return {r["sha256"]: r for r in rows(self.dst, table)}

    # ---------------------------------------------------------------- tables
    def standards(self) -> None:
        print("Standards")
        by_sha, ids = self._by_sha("standards"), self._ids("standards")
        new: list[dict] = []
        for r in rows(self.src, "standards"):
            if r["sha256"] in by_sha:
                self.map_std[r["id"]] = by_sha[r["sha256"]]["id"]
                self._count("standards", "same file already there")
                continue
            if r["id"] in ids:
                print(f"    ! id clash {r['id']} with a different file - skipped")
                continue
            self.map_std[r["id"]] = r["id"]
            src_file = self._find_src_file(r["storage_key"], r["sha256"])
            r["storage_key"] = self._free_key(r["storage_key"], r["id"])
            if self._copy(src_file, r["storage_key"]):
                print(f"  + {r['name']}:{r.get('edition') or ''} ({r['part_type']})")
                new.append(r)
        for r in new:                               # parents may point to rows of either data set
            r["parent_id"] = self.map_std.get(r.get("parent_id")) if r.get("parent_id") else None
            self._insert("standards", r)
            self._count("standards", "added")
        if not self.dry:
            self._link_parents([r["id"] for r in new])

    def _link_parents(self, new_ids: list[str]) -> None:
        """Part 2 without a parent -> newest Part 1 of the same family (same rule as the app)."""
        part1 = {}
        for r in self.dst.execute("SELECT id, family_key, edition FROM standards WHERE part_type='part1' "
                                  "ORDER BY created_at"):
            part1[r[1]] = r[0]
        for sid, fam in self.dst.execute("SELECT id, family_key FROM standards WHERE part_type='part2' "
                                         "AND parent_id IS NULL").fetchall():
            if fam in part1:
                self.dst.execute("UPDATE standards SET parent_id=? WHERE id=?", (part1[fam], sid))
                print(f"    linked Part 2 {sid[:8]} -> Part 1 {part1[fam][:8]}")

    def report_files(self) -> None:
        print("Report files")
        by_sha, ids = self._by_sha("report_files"), self._ids("report_files")
        for r in rows(self.src, "report_files"):
            src_folder = report_dir(r["storage_key"])
            if r["sha256"] in by_sha:
                t = by_sha[r["sha256"]]
                self.map_rf[r["id"]] = t["id"]
                self.folder[src_folder] = report_dir(t["storage_key"])
                self._count("report_files", "same file already there")
                continue
            if r["id"] in ids:
                print(f"    ! id clash {r['id']} with a different file - skipped")
                continue
            src_file = self._find_src_file(r["storage_key"], r["sha256"])
            r["storage_key"] = self._free_key(r["storage_key"], r["id"])
            if not self._copy(src_file, r["storage_key"]):
                continue
            self.map_rf[r["id"]] = r["id"]
            self.folder[src_folder] = report_dir(r["storage_key"])
            print(f"  + {r['original_filename']}")
            self._insert("report_files", r)
            self._count("report_files", "added")

    def chats(self) -> None:
        print("Chats")
        ids = self._ids("chat_sessions")
        for r in rows(self.src, "chat_sessions"):
            if r["id"] in ids:
                continue
            r["active_report_file_id"] = self.map_rf.get(r.get("active_report_file_id"))
            r["active_standard_ids"] = [self.map_std[s] for s in jload(r.get("active_standard_ids"), [])
                                        if s in self.map_std]
            self._insert("chat_sessions", r)
            self._count("chat_sessions", "added")

    def _with_parents(self, std_ids: list[str]) -> list[str]:
        """Same selection as the app: every Part 2 brings its Part 1 parent."""
        out = list(dict.fromkeys(std_ids))
        for sid in list(out):
            row = self.dst.execute("SELECT parent_id FROM standards WHERE id=?", (sid,)).fetchone()
            if row and row[0] and row[0] not in out:
                out.append(row[0])
        return out

    def _names(self, std_ids: list[str]) -> list[str]:
        names = []
        for sid in std_ids:
            row = self.dst.execute("SELECT name, edition FROM standards WHERE id=?", (sid,)).fetchone()
            if row:
                names.append(f"{row[0]}:{row[1]}" if row[1] else row[0])
        return names

    def _move_key(self, key: str | None) -> str | None:
        """Output path in the source report folder -> same file name in the target report folder."""
        if not key:
            return key
        for old, new in self.folder.items():
            if key.startswith(old):
                return new + key[len(old):]
        return key

    def _copy_output(self, src_key: str | None, dst_key: str | None, with_siblings: bool = False) -> None:
        if not src_key or not dst_key:
            return
        if with_siblings:                          # evaluation folder: pdf + xlsx + json
            src_dir = (self.src_storage / src_key).parent
            for f in sorted(src_dir.glob("*")) if src_dir.is_dir() else []:
                self._copy(f, dst_key.rsplit("/", 1)[0] + "/" + f.name)
        else:
            self._copy(self._find_src_file(src_key), dst_key)

    def answers(self) -> dict[str, str]:
        """reports table (stored chat answers). Returns source standards_key -> new standards_key."""
        print("Stored answers (reports)")
        ids, rekey = self._ids("reports"), {}
        chat_ids = self._ids("chat_sessions") if not self.dry else self._ids("chat_sessions") | {
            r["id"] for r in rows(self.src, "chat_sessions")}
        for r in rows(self.src, "reports"):
            if r["id"] in ids:
                continue
            if r["report_file_id"] not in self.map_rf:
                print(f"    ! answer {r['id'][:8]}: its report file was not copied - skipped")
                continue
            if r["chat_id"] not in chat_ids:
                print(f"    ! answer {r['id'][:8]}: its chat is missing - skipped")
                continue
            std = self._with_parents([self.map_std[s] for s in jload(r.get("standard_ids"), []) if s in self.map_std])
            new_key = ",".join(sorted(std))
            rekey[r["standards_key"]] = new_key
            r.update(report_file_id=self.map_rf[r["report_file_id"]], standard_ids=std,
                     standard_names=self._names(std), standards_key=new_key)
            old_path = r.get("output_path")
            r["output_path"] = self._move_key(old_path)
            self._copy_output(old_path, r["output_path"])
            print(f"  + answer {r['id'][:8]} for {r['report_name']} ({', '.join(r['standard_names'])})")
            self._insert("reports", r)
            self._count("reports", "added")
        return rekey

    def messages(self) -> None:
        ids, rep_ids = self._ids("chat_messages"), self._ids("reports")
        for r in rows(self.src, "chat_messages"):
            if r["id"] in ids:
                continue
            if r.get("report_id") and r["report_id"] not in rep_ids and not self.dry:
                r["report_id"] = None
            self._insert("chat_messages", r)
            self._count("chat_messages", "added")

    def evaluations(self, rekey: dict[str, str]) -> None:
        print("Evaluations (PDF / XLSX / JSON)")
        ids = self._ids("evaluations")
        chat_ids = self._ids("chat_sessions")
        for r in rows(self.src, "evaluations"):
            if r["id"] in ids:
                continue
            if r["report_file_id"] not in self.map_rf:
                print(f"    ! evaluation {r['id'][:8]}: its report file was not copied - skipped")
                continue
            std = self._with_parents([self.map_std[s] for s in jload(r.get("standard_ids"), []) if s in self.map_std])
            base, _, tail = (r.get("standards_key") or "").partition("|")
            r.update(report_file_id=self.map_rf[r["report_file_id"]], standard_ids=std,
                     standard_names=self._names(std),
                     standards_key=rekey.get(base, ",".join(sorted(std))) + (f"|{tail}" if tail else ""),
                     chat_id=r.get("chat_id") if r.get("chat_id") in chat_ids or self.dry else None)
            for col in ("pdf_path", "xlsx_path"):
                old = r.get(col)
                r[col] = self._move_key(old)
                self._copy_output(old, r[col], with_siblings=(col == "pdf_path"))
            self._insert("evaluations", r)
            self._count("evaluations", "added")

    def run(self) -> None:
        self.standards()
        self.report_files()
        self.chats()
        rekey = self.answers()
        self.messages()
        self.evaluations(rekey)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source-db", required=True, type=Path)
    ap.add_argument("--source-storage", required=True, type=Path)
    ap.add_argument("--target-db", type=Path, default=ROOT / "data" / "complify.db")
    ap.add_argument("--target-storage", type=Path, default=ROOT / "storage")
    ap.add_argument("--dry-run", action="store_true", help="show what would be added, change nothing")
    a = ap.parse_args()

    for p in (a.source_db, a.target_db):
        if not p.is_file():
            sys.exit(f"Not found: {p}")
    if a.source_db.resolve() == a.target_db.resolve():
        sys.exit("Source and target DB are the same file")

    src = open_source(a.source_db)
    if not a.dry_run:
        backup = a.target_db.with_name(a.target_db.name + time.strftime(".bak-%Y%m%d-%H%M%S"))
        shutil.copy2(a.target_db, backup)
        print(f"Backup of target DB: {backup}")
    dst = sqlite3.connect(a.target_db)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    m = Merger(src, a.source_storage, dst, a.target_storage, a.dry_run)
    try:
        m.run()
        if a.dry_run:
            dst.rollback()
        else:
            dst.commit()
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()

    print("\nSummary" + (" (dry run, nothing changed)" if a.dry_run else ""))
    for table, s in m.stats.items():
        print(f"  {table:15s} " + ", ".join(f"{k}: {v}" for k, v in s.items()))
    print(f"  files copied    {m.copied_files}")


if __name__ == "__main__":
    main()