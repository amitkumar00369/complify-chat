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
    download_json: str = ""


class ChatResponse(BaseModel):
    chat_id: str
    user_id: str
    new_thread: bool                 # true = a new chat thread was created for this request
    user_evaluation_id: str          # id of the user's request message in this chat
    report_analysis_id: str          # id of the stored answer message for this report
    result: dict | None = None       # evaluation as JSON (report / summary / findings) - same as /evaluations/{id}/json
    answer: str | None = None        # plain text only for follow-up questions (no evaluation)
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


class EvaluationFiles(BaseModel):
    download_json: str
    download_pdf: str


class ChatPublicResponse(BaseModel):
    """POST /chat response. Same as ChatResponse (POST /chat/full) except: chat_id -> session_id,
    no warnings, result without report_id, evaluation = only the JSON and PDF links."""
    session_id: str                  # send it back as session_id to ask follow-up questions in the same chat
    user_id: str
    new_thread: bool
    user_evaluation_id: str
    report_analysis_id: str
    result: dict | None = None       # gaps + final_summary (evaluation) ; None for a follow-up question
    answer: str | None = None        # text answer of a follow-up question
    from_cache: bool
    analysis_id: str | None
    output_path: str | None = None
    report: ReportFileOut | None
    standards_used: list[StandardRef]
    standards_auto_detected: bool
    usage: Usage | None = None
    reused_existing_output: bool = False
    evaluation: EvaluationFiles | None = None

    @classmethod
    def from_full(cls, full: "ChatResponse") -> "ChatPublicResponse":
        data = full.model_dump(exclude={"chat_id", "warnings", "evaluation", "result"})
        result = {k: v for k, v in full.result.items() if k != "report_id"} if full.result else None
        ev = full.evaluation
        return cls(session_id=full.chat_id, result=result,
                   evaluation=EvaluationFiles(download_json=ev.download_json, download_pdf=ev.download_pdf)
                   if ev else None, **data)


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