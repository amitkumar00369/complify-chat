# Complify Chat API

FastAPI service. You upload standards (Part 1 / Part 2-x) and test reports, then chat
about a report the way ChatGPT does. The chat remembers context through the OpenAI
Conversations API, and repeated prompts are answered from SQLite without calling OpenAI.

**Storage right now: local disk only.** Only PDFs (and the result `.md` files) go on disk.
There are no JSON sidecar files; all metadata lives in SQLite.

## Disk layout

```
storage/
├── standards/                                   # standards are related (parent-child)
│   └── IEC_60335/                               # family
│       ├── part1/
│       │   ├── IEC_60335-1_2010.pdf             # parent (general requirements)
│       │   └── IEC_60335-1_2020.pdf
│       └── part2/
│           ├── IEC_60335-2-7_2012.pdf           # child -> IEC 60335-1:2010
│           └── IEC_60335-2-24_2020.pdf          # child -> IEC 60335-1
└── reports/                                     # each report kept separately
    └── {report_id}/
        ├── washer_report.pdf
        └── outputs/
            ├── {analysis_id}.md                 # result of one prompt
            └── {analysis_id}.md
```

Other naming patterns:
- `IEC 61000-3-2` goes to `IEC_61000/other/`. No parent linking for these.
- `UL 2157` (no part number) goes directly in `UL_2157/`.

## Parent-child rules (Part 1 / Part 2)

| Situation | What happens |
|---|---|
| Part 2 uploaded, Part 1 already present | `parent_id` is set. If the Part 2 PDF says "use with IEC 60335-1:2010", **that edition** is preferred |
| Part 2 uploaded, Part 1 missing | Saved anyway with `parent_id = null`, plus a warning |
| Part 1 uploaded later | All Part 2 files of that family get linked (or re-linked to the correct edition) automatically |
| Chat uses a Part 2 (auto-detected, file or id) | Its Part 1 parent is added automatically: `[Part 1, Part 2]` |
| Report itself names a Part 1 edition | The report's edition wins over the linked parent |

The OpenAI prompt is told that Part 2 clauses replace, modify or add to the Part 1 clauses
with the same number, and that Part 1 applies wherever Part 2 is silent.

## Folder structure (code)

```
complify-chat/
├── main.py  .env  requirements.txt
├── data/complify.db        # SQLite (auto-created, outside app/)
├── storage/                # PDFs + outputs (auto-created)
└── app/
    ├── api/                # routes + dependency injection
    ├── controller/         # input validation, response building
    ├── service/            # standard_service (parent-child), report_service (cache + output files),
    │                       # chat_service, ai_service (OpenAI), analysis_service (chat flow), storage_service
    ├── middleware/         # rate_limit, request_timing
    ├── models/  schema/  db/  config/
    └── utils/              # pdf_utils (detect + parse standard names), text_utils, exceptions
```

## Tables

| Table | Purpose |
|---|---|
| `standards` | name, edition, **family, part_type, part_number, parent_id**, parent_edition_hint, storage_key, sha256 |
| `report_files` | report PDF: storage_key, sha256, detected standards (`IEC 60335-2-7:2012`) |
| `reports` | each analysis: report + standards used + prompt + output + **output_path**. This is the cache |
| `chat_sessions` | user_id, OpenAI conversation id, active report/standards |
| `chat_messages` | every user and assistant turn |

## Run

```bash
python -m venv .venv && .venv\Scripts\activate      # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set OPENAI_API_KEY
python main.py                # http://localhost:8000/docs
```

> Upgrading from the earlier S3 version: delete `data/complify.db` once, because the table columns changed.

## APIs

### Standards
```bash
curl -X POST localhost:8000/api/v1/standards -F "file=@IEC_60335-1_2010.pdf"
curl -X POST localhost:8000/api/v1/standards -F "file=@IEC_60335-2-7.pdf"     # name/edition optional
```
The response contains `standard` (with `storage_key`, `part_type`, `parent_id`), `parent`,
`children`, `duplicate` and `warnings`.

- `GET /api/v1/standards?family=IEC 60335`
- `GET /api/v1/standards/tree`: family, then part1 / part2 / other
- `GET /api/v1/standards/{id}`: the standard with its parent and children

### Chat
`POST /api/v1/chat` (multipart): `user_id`*, `query`*, `chat_id`, `report_file` (1),
`standard_files` (up to 2), `standard_ids`, `use_cache`

```bash
curl -X POST localhost:8000/api/v1/chat -F user_id=amit -F "query=Evaluate this report" \
     -F "report_file=@washer_report.pdf"
curl -X POST localhost:8000/api/v1/chat -F user_id=amit -F chat_id=<id> -F "query=Why did clause 19.7 fail?"
```
The response includes `standards_used` (with part_type and parent_id), `output_path`
(the `.md` result file) and `warnings`.

History endpoints:
- `GET /api/v1/chats?user_id=`
- `GET /api/v1/chats/{chat_id}/messages?user_id=`
- `GET /api/v1/reports?user_id=`

## Notes
- **Cache:** the key is report + standards + normalised prompt, and the default is exact match. A context-dependent
  follow-up ("explain more") can hit another chat's cached answer, so send `use_cache=false` for those.
- **Limits:** a user can send up to 2 standards. After parents are added, up to 3 can be in context
  (`MAX_STANDARDS_IN_CONTEXT`).
- **Switching to S3 later:** set `STORAGE_BACKEND=s3` and fill the bucket settings. The same folder layout becomes the S3 keys.
- **Auth:** none yet. `user_id` comes from the client.

## Evaluation report (Excel + PDF, golden-workbook format)

`POST /api/v1/evaluations` (multipart) produces a formal technical evaluation in the same
structure as the client's golden evaluation workbook. Fields:
- `user_id`
- a report: `report_file`, or `report_id`, or `chat_id`
- optional: `standard_files`, `standard_ids`, `use_cache`

| Step | How |
|---|---|
| Resolve report + standards (Part 2 brings Part 1) | deterministic |
| Clause register from the TRF table (clause, verdict, pages) | deterministic (`trf_parser.py`, pdfplumber, line-based) |
| Identity/traceability, decision points, critical findings, Part 2 coverage, Part 1 corrections, clause notes | **one structured OpenAI call** (`EvaluationAI` JSON schema, engineer writing rules) |
| Clause-by-clause assessment (fixed vocabulary + AI notes) | deterministic (`clause_review.py`) |
| Excel (5 sheets) + branded PDF (cover, sections, annex, sign-off) | `render/excel_renderer.py`, `render/pdf_renderer.py` |

Output files are written to `storage/reports/{report_id}/outputs/evaluation_{id}/`, as
`Technical_Evaluation_<report_no>.pdf` and `.xlsx`.

The structured result is stored in the `evaluations` table, so `POST /evaluations/{id}/render` re-creates
the files without calling the AI. Use it after you change the PDF format or the branding (`BRAND_*` in `.env`).

Other endpoints:
- `GET /evaluations/{id}/download/pdf|xlsx`
- `GET /evaluations?user_id=`
- `GET /evaluations/{id}`

Same report + same standards are served from cache unless you send `use_cache=false`.

## Postman
Import `postman/Complify_Chat_API.postman_collection.json`. Set `base_url` and `user_id` in the
collection variables, then run folders 1 → 2 → 3. The IDs get saved automatically. You have to select the PDF in each file field yourself.

## Tests
```bash
pytest -q      # local temp storage + fake OpenAI, no keys needed
```
