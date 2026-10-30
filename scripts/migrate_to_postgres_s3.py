"""Copy local data to PostgreSQL + S3 so stored evaluations are reused (no new AI calls).

    SQLite  data/complify.db   ->  PostgreSQL  (DATABASE_URL in .env, or --database-url)
    folder  storage/...        ->  S3 bucket   (S3_BUCKET / AWS_* in .env), SAME keys

Because the S3 keys are identical to the local paths (standards/..., reports/{id}/...), every
storage_key / pdf_path / xlsx_path / output_path in the database stays valid.

Safe to run more than once: rows whose id already exists are skipped, files already in S3 are skipped.

Usage (from the project folder):
    python scripts/migrate_to_postgres_s3.py --dry-run          # show what would be copied
    python scripts/migrate_to_postgres_s3.py                    # copy DB + files
    python scripts/migrate_to_postgres_s3.py --skip-files       # DB only
    python scripts/migrate_to_postgres_s3.py --skip-db          # files only
"""
import argparse
import asyncio
import mimetypes
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, insert, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.db.database import Base, async_url  # noqa: E402
import app.models  # noqa: E402,F401  (register tables)

# parents before children (foreign keys)
TABLE_ORDER = ["standards", "report_files", "chat_sessions", "reports", "chat_messages", "evaluations"]
PATH_COLUMNS = {"standards": ["storage_key"], "report_files": ["storage_key"],
                "reports": ["output_path"], "evaluations": ["pdf_path", "xlsx_path"]}


def log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ database
def read_source(sqlite_path: Path) -> dict[str, list[dict]]:
    eng = create_engine(f"sqlite:///{sqlite_path.as_posix()}")
    data = {}
    with eng.connect() as conn:
        for name in TABLE_ORDER:
            table = Base.metadata.tables[name]
            rows = [dict(r._mapping) for r in conn.execute(select(table))]
            for r in rows:                         # SQLite returns naive datetimes; Postgres wants UTC-aware
                for k, v in r.items():
                    if hasattr(v, "tzinfo") and hasattr(v, "hour") and v.tzinfo is None:
                        r[k] = v.replace(tzinfo=timezone.utc)
            if name == "standards":                # Part 1 before Part 2 (self reference)
                rows.sort(key=lambda r: r["parent_id"] is not None)
            data[name] = rows
    eng.dispose()
    return data


async def write_target(database_url: str, data: dict[str, list[dict]], dry_run: bool) -> dict[str, tuple[int, int]]:
    engine = create_async_engine(async_url(database_url))
    result = {}
    try:
        async with engine.begin() as conn:
            if not dry_run:
                await conn.run_sync(Base.metadata.create_all)
            for name in TABLE_ORDER:
                table = Base.metadata.tables[name]
                rows = data[name]
                try:
                    existing = {r[0] for r in await conn.execute(select(table.c.id))}
                except Exception:                  # table not created yet (dry run on empty DB)
                    existing = set()
                new = [r for r in rows if r["id"] not in existing]
                if new and not dry_run:
                    for i in range(0, len(new), 500):
                        await conn.execute(insert(table), new[i:i + 500])
                result[name] = (len(rows), len(new))
                log(f"  {name:15} source={len(rows):5}  new={len(new):5}  already-there={len(rows) - len(new):5}")
    finally:
        await engine.dispose()
    return result


# ------------------------------------------------------------------ files
def s3_client(s):
    import boto3

    return boto3.client("s3", region_name=s.aws_region, aws_access_key_id=s.aws_access_key_id or None,
                        aws_secret_access_key=s.aws_secret_access_key or None,
                        endpoint_url=s.s3_endpoint_url or None)


def s3_has(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def copy_files(storage_dir: Path, bucket: str, client, dry_run: bool, workers: int = 8) -> tuple[int, int, list[str]]:
    files = [p for p in storage_dir.rglob("*") if p.is_file()]
    uploaded, skipped, errors = 0, 0, []

    def one(path: Path):
        key = path.relative_to(storage_dir).as_posix()
        if s3_has(client, bucket, key):
            return "skip", key
        if dry_run:
            return "new", key
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".md":
            ctype = "text/markdown"
        client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), ContentType=ctype,
                          ServerSideEncryption="AES256")
        return "new", key

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in [pool.submit(one, p) for p in files]:
            try:
                status, key = fut.result()
                uploaded += status == "new"
                skipped += status == "skip"
            except Exception as exc:
                errors.append(str(exc))
    return uploaded, skipped, errors


def verify_paths(data: dict[str, list[dict]], bucket: str, client) -> list[str]:
    missing = []
    for table, cols in PATH_COLUMNS.items():
        for r in data[table]:
            for c in cols:
                key = r.get(c)
                if key and not s3_has(client, bucket, key):
                    missing.append(f"{table}.{c}: {key}")
    return missing


# ------------------------------------------------------------------ main
def main() -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", default=str(ROOT / "data" / "complify.db"), help="source SQLite file")
    ap.add_argument("--storage-dir", default=str(s.local_storage_dir), help="source storage folder")
    ap.add_argument("--database-url", default=s.database_url, help="target PostgreSQL URL")
    ap.add_argument("--bucket", default=s.s3_bucket, help="target S3 bucket")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-db", action="store_true")
    ap.add_argument("--skip-files", action="store_true")
    a = ap.parse_args()

    sqlite_path, storage_dir = Path(a.sqlite), Path(a.storage_dir)
    if not sqlite_path.exists():
        log(f"Source DB not found: {sqlite_path}")
        return 1
    if not a.skip_db and "postgres" not in a.database_url:
        log(f"Target DATABASE_URL is not PostgreSQL: {a.database_url}  (set it in .env or pass --database-url)")
        return 1
    if not a.skip_files and not a.bucket:
        log("S3_BUCKET is not set (set it in .env or pass --bucket)")
        return 1

    log(f"Source : {sqlite_path}  +  {storage_dir}")
    log(f"Target : {a.database_url.split('@')[-1]}  +  s3://{a.bucket}")
    log("Mode   : DRY RUN (nothing is written)" if a.dry_run else "Mode   : COPY")

    data = read_source(sqlite_path)
    if not a.skip_db:
        log("\nDatabase:")
        asyncio.run(write_target(a.database_url, data, a.dry_run))

    if not a.skip_files:
        client = s3_client(s)
        log("\nFiles:")
        if storage_dir.exists():
            up, sk, errs = copy_files(storage_dir, a.bucket, client, a.dry_run)
            log(f"  {'to upload' if a.dry_run else 'uploaded'}={up}  already-in-S3={sk}  errors={len(errs)}")
            for e in errs[:10]:
                log(f"    ERROR {e}")
        else:
            log(f"  storage folder not found: {storage_dir}")
        if not a.dry_run:
            missing = verify_paths(data, a.bucket, client)
            log(f"\nVerify: {'all referenced files are in S3' if not missing else f'{len(missing)} referenced file(s) MISSING'}")
            for m in missing[:20]:
                log(f"    MISSING {m}")
            if missing:
                return 2
    log("\nDone. Now set STORAGE_BACKEND=s3 and DATABASE_URL=postgresql+asyncpg://... in .env and restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())