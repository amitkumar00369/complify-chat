from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StandardRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    edition: str | None


class StandardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    edition: str | None
    family: str
    part_type: str            # part1 | part2 | part | standalone
    part_number: str | None
    parent_id: str | None
    parent_edition_hint: str | None
    original_filename: str
    storage_key: str          # standards/IEC_60335/part2/IEC_60335-2-7_2012.pdf
    size_bytes: int
    created_at: datetime


class StandardUploadResponse(BaseModel):
    standard: StandardOut
    duplicate: bool                       # same file already uploaded -> nothing new stored
    parent: StandardRefOut | None = None  # Part 1 of a Part 2
    children: list[StandardRefOut] = []   # Part 2s linked to a Part 1
    warnings: list[str] = []


class StandardFamilyOut(BaseModel):
    family: str
    part1: list[StandardOut]
    part2: list[StandardOut]
    other: list[StandardOut]
