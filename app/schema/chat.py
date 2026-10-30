from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StandardRef(BaseModel):
    id: str
    name: str
    edition: str | None = None
    part_type: str | None = None
    parent_id: str | None = None


class ReportFileOut(BaseModel):
    id: str
    filename: str
    detected_standards: list[str]


class Usage(BaseModel):
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatEvaluationOut(BaseModel):
    evaluation_id: str
    final_verdict: str | None
    pdf_path: str | None
    xlsx_path: str | None
    download_pdf: str
    download_xlsx: str


class ChatResponse(BaseModel):
    chat_id: str
    user_id: str
    new_chat: bool
    user_message_id: str
    assistant_message_id: str
    answer: str
    from_cache: bool
    analysis_id: str | None
    output_path: str | None = None   # reports/{report_id}/outputs/{analysis_id}.md
    report: ReportFileOut | None
    standards_used: list[StandardRef]
    standards_auto_detected: bool
    warnings: list[str] = []
    usage: Usage | None = None
    reused_existing_output: bool = False          # same report evaluated before -> no AI call, $0
    evaluation: ChatEvaluationOut | None = None   # Excel + PDF built from this answer


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    title: str
    active_report_file_id: str | None
    active_standard_ids: list[str]
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    report_id: str | None
    attachments: list[str]
    from_cache: bool
    created_at: datetime


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    report_name: str
    standard_names: list[str]
    prompt: str
    output: str
    output_path: str | None
    model: str | None
    cache_hits: int
    created_at: datetime
