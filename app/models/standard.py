from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.base import new_id, utcnow


class Standard(Base):
    """A standard PDF stored on disk.

    Parent-child: Part 2-x (particular requirements, e.g. IEC 60335-2-7) points
    to its Part 1 (general requirements, IEC 60335-1) via parent_id.
    """

    __tablename__ = "standards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), index=True)          # "IEC 60335-2-7"
    name_key: Mapped[str] = mapped_column(String(255), index=True)      # "IEC6033527"
    edition: Mapped[str | None] = mapped_column(String(50))             # "2012"
    family: Mapped[str] = mapped_column(String(255))                    # "IEC 60335"
    family_key: Mapped[str] = mapped_column(String(255), index=True)    # "IEC60335"
    part_type: Mapped[str] = mapped_column(String(20), index=True)      # part1 | part2 | part | standalone
    part_number: Mapped[str | None] = mapped_column(String(50))         # "1", "2-7"
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("standards.id", ondelete="SET NULL"), index=True
    )
    # Part 1 edition named inside the Part 2 PDF ("use with IEC 60335-1:2010")
    parent_edition_hint: Mapped[str | None] = mapped_column(String(50))
    original_filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(1000))   # standards/IEC_60335/part2/IEC_60335-2-7_2012.pdf
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    openai_file_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
