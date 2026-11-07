---
skill_id: data_extraction
version: 1.0.0
spec_ref: docs/spec.md#51-dataextractionagent
task_ids: [TASK-ETL-001, TASK-ETL-002, TASK-ETL-003, TASK-ETL-004, TASK-ETL-005, TASK-ETL-006, TASK-ETL-007, TASK-ETL-008]
status: APPROVED
owner: DataExtractionAgent
dependencies: []
---

# Skill: Data Extraction

## Purpose

Provides atomic, reusable data extraction capabilities for structured and semi-structured content from enterprise document formats. This skill is consumed by `DataExtractionAgent` and `FinancialReconAgent`.

---

## Supported Document Types

| Format      | Handler                        | Confidence Baseline | Notes                             |
|-------------|--------------------------------|--------------------|------------------------------------|
| PDF (text)  | `pypdf2` / `pdfminer`          | 0.95               | Native text layer extraction       |
| PDF (scan)  | Tesseract OCR + post-process   | 0.78               | Quality depends on scan resolution |
| CSV         | `pandas.read_csv`              | 0.99               | Schema inference enabled           |
| XLSX        | `openpyxl`                     | 0.98               | Multi-sheet support                |
| Email (EML) | `email.parser`                 | 0.92               | Attachments extracted recursively  |
| HTML        | `BeautifulSoup4`               | 0.90               | Table detection and extraction     |
| JSON        | Native parse                   | 0.99               | Schema flattening supported        |
| Plain Text  | Regex + LLM-assisted           | 0.85               | LLM used for field disambiguation  |

---

## Skill Steps (Atomic Operations)

### Step 1: Document Ingestion (`STEP-ETL-01`)

**Task IDs**: TASK-ETL-001, TASK-ETL-002

**Description**: Accept raw document input, detect format, and prepare for parsing.

**Input**:
```json
{
  "source": "string (URI, file path, or base64 inline)",
  "source_type": "string (auto | pdf | csv | xlsx | email | html | json | text)"
}
```

**Output**:
```json
{
  "document_id": "string (SHA256 of content)",
  "detected_type": "string",
  "page_count": 0,
  "size_bytes": 0,
  "raw_content": "string"
}
```

**Idempotency**: Identical `source` → identical `document_id`. Safe to call multiple times.

---

### Step 2: Content Normalization (`STEP-ETL-02`)

**Task IDs**: TASK-ETL-003

**Description**: Normalize raw content — clean whitespace, decode character encodings, standardize numeric formats, detect language.

**Input**:
```json
{
  "raw_content": "string",
  "detected_type": "string",
  "locale_hint": "string (default: en-US)"
}
```

**Output**:
```json
{
  "normalized_content": "string",
  "detected_language": "string (BCP-47)",
  "encoding_used": "string",
  "numeric_locale": "string"
}
```

---

### Step 3: Field Extraction (`STEP-ETL-03`)

**Task IDs**: TASK-ETL-004, TASK-ETL-005

**Description**: Extract named fields from normalized content using a combination of:
1. Rule-based regex patterns (high precision, known fields)
2. LLM-assisted extraction (flexible, unknown layouts)
3. Table detection (structured tabular data)

**Input**:
```json
{
  "normalized_content": "string",
  "target_schema": {
    "type": "object",
    "properties": {}
  },
  "extraction_hints": ["array of field names to prioritize"],
  "extraction_strategy": "enum[rules_first, llm_first, hybrid]"
}
```

**Output**:
```json
{
  "extracted_fields": {},
  "raw_evidence": {
    "field_name": {
      "source_text": "string",
      "page_num": 0,
      "bounding_box": {"x1": 0, "y1": 0, "x2": 0, "y2": 0}
    }
  }
}
```

---

### Step 4: Confidence Scoring (`STEP-ETL-04`)

**Task IDs**: TASK-ETL-006

**Description**: Assign per-field confidence scores. Flag fields below threshold for human review.

**Input**:
```json
{
  "extracted_fields": {},
  "raw_evidence": {},
  "confidence_threshold": 0.85
}
```

**Output**:
```json
{
  "confidence_scores": {"field_name": 0.0},
  "low_confidence_fields": ["array of field names"],
  "overall_confidence": 0.0,
  "extraction_warnings": ["array of warning messages"]
}
```

**Confidence Scoring Rules**:
- Rule-based extraction with exact regex match: `0.98`
- Rule-based extraction with fuzzy match: `0.85 - 0.97`
- LLM extraction with grounding evidence: `0.75 - 0.92`
- LLM extraction without evidence: `0.50 - 0.75`
- OCR-derived field: apply `0.85 * base_confidence`

---

### Step 5: Schema Validation (`STEP-ETL-05`)

**Task IDs**: TASK-ETL-007, TASK-ETL-008

**Description**: Validate extracted data against target JSON Schema. Coerce types where safe; reject invalid records.

**Input**:
```json
{
  "extracted_fields": {},
  "target_schema": {},
  "coerce_types": true
}
```

**Output**:
```json
{
  "validated_data": {},
  "validation_errors": [
    {"field": "string", "error": "string", "value_found": "any"}
  ],
  "is_valid": true,
  "coercions_applied": ["array of field names where type was coerced"]
}
```

---

## Error Codes

| Code             | Description                                    | Retry? |
|------------------|------------------------------------------------|--------|
| `ETL-001`        | Unsupported document format                    | No     |
| `ETL-002`        | OCR confidence below minimum (< 0.60)          | No     |
| `ETL-003`        | LLM extraction timeout                         | Yes    |
| `ETL-004`        | Schema validation failure (non-coercible)      | No     |
| `ETL-005`        | Document exceeds size limit (> 50MB)           | No     |
| `ETL-006`        | Source URI unreachable                         | Yes    |

---

## Configuration

```yaml
# skills/config/data_extraction.yaml
data_extraction:
  confidence_threshold: 0.85
  ocr_min_confidence: 0.60
  max_document_size_mb: 50
  extraction_strategy: hybrid
  llm_model: gemini-2.5-pro
  llm_max_tokens: 4096
  llm_temperature: 0.1  # Low temperature for deterministic extraction
  supported_locales: [en-US, en-GB, de-DE, fr-FR, ja-JP]
  cache_enabled: true
  cache_ttl_seconds: 3600
```

---

## Observability Events

| Event Name                     | Trigger                              | Key Attributes                                |
|--------------------------------|--------------------------------------|-----------------------------------------------|
| `extraction.started`           | Step 1 begins                        | `document_id`, `document_type`, `size_bytes`  |
| `extraction.normalized`        | Step 2 completes                     | `document_id`, `detected_language`            |
| `extraction.fields_extracted`  | Step 3 completes                     | `document_id`, `field_count`, `strategy_used` |
| `extraction.low_confidence`    | Any field below threshold            | `document_id`, `field_name`, `confidence`     |
| `extraction.completed`         | Step 5 completes                     | `document_id`, `is_valid`, `overall_confidence`|
| `extraction.error`             | Any step fails                       | `document_id`, `error_code`, `step_id`        |

---

## Usage Example

```python
from skills.data_extraction import DataExtractionSkill

skill = DataExtractionSkill(config_path="skills/config/data_extraction.yaml")

result = skill.run(
    source="s3://invoices/invoice_2026_Q4_001.pdf",
    source_type="pdf",
    target_schema={
        "type": "object",
        "properties": {
            "invoice_number": {"type": "string"},
            "total_amount": {"type": "number"},
            "vendor_name": {"type": "string"},
            "invoice_date": {"type": "string", "format": "date"}
        },
        "required": ["invoice_number", "total_amount", "vendor_name"]
    },
    confidence_threshold=0.85
)

print(result.validated_data)
print(result.low_confidence_fields)  # Fields requiring human review
```

---

## Test Coverage Requirements

- [ ] `TC-ETL-001`: PDF text extraction — perfect layout (confidence ≥ 0.95)
- [ ] `TC-ETL-002`: PDF OCR extraction — scanned document (confidence ≥ 0.78)
- [ ] `TC-ETL-003`: CSV with missing columns — coercion + validation
- [ ] `TC-ETL-004`: Email with PDF attachment — recursive extraction
- [ ] `TC-ETL-005`: HTML table extraction — multi-table document
- [ ] `TC-ETL-006`: Schema validation failure — non-coercible type mismatch
- [ ] `TC-ETL-007`: Idempotency check — same input, same `document_id`
- [ ] `TC-ETL-008`: Large document (45MB PDF) — size limit boundary test
