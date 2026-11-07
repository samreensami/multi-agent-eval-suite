---
skill_id: report_generation
version: 1.0.0
spec_ref: docs/spec.md#53-reportgenagent
task_ids: [TASK-RPT-001, TASK-RPT-002, TASK-RPT-003, TASK-RPT-004, TASK-RPT-005, TASK-RPT-006]
status: APPROVED
owner: ReportGenAgent
dependencies: [data_extraction, financial_reconciliation]
---

# Skill: Report Generation

## Purpose

Provides structured, citation-grounded business report generation across multiple output formats. Synthesizes outputs from upstream agents (DataExtractionAgent, FinancialReconAgent) into human-ready executive deliverables. Implements hallucination detection to ensure all generated claims are grounded in provided data sources.

---

## Report Types

| Report Type               | Task ID       | Audience          | Avg. Length | Output Formats      |
|---------------------------|---------------|-------------------|-------------|---------------------|
| `executive_summary`       | TASK-RPT-001  | C-Suite           | 500-800w    | PDF, DOCX           |
| `audit_report`            | TASK-RPT-002  | Auditors, Legal   | 2000-5000w  | PDF, HTML           |
| `operational_dashboard`   | TASK-RPT-003  | Operations Mgr    | 300-500w    | HTML, Markdown      |
| `financial_statement`     | TASK-RPT-004  | Finance, Board    | 1000-3000w  | PDF, XLSX           |
| `reconciliation_report`   | TASK-RPT-005  | AP/AR Teams       | 500-1500w   | PDF, Markdown       |
| `benchmark_report`        | TASK-RPT-006  | Engineering, PM   | 800-2000w   | Markdown, HTML      |

---

## Skill Steps (Atomic Operations)

### Step 1: Data Source Aggregation (`STEP-RPT-01`)

**Task IDs**: TASK-RPT-001, TASK-RPT-002

**Description**: Collect and validate all data sources required for report generation. Build a unified data context.

**Input**:
```json
{
  "data_sources": [
    {
      "source_id": "string (unique within report)",
      "source_type": "enum[agent_output, database_query, api_response, document]",
      "source_citation": "string (human-readable reference)",
      "data": {},
      "timestamp_utc": "string",
      "confidence": 0.0
    }
  ],
  "report_type": "string",
  "fiscal_period": "YYYY-QN"
}
```

**Output**:
```json
{
  "unified_context": {
    "data_map": {"source_id": {}},
    "citation_registry": {"source_id": "citation_string"},
    "data_quality_summary": {
      "total_sources": 0,
      "avg_confidence": 0.0,
      "low_quality_sources": []
    }
  }
}
```

---

### Step 2: Outline Generation (`STEP-RPT-02`)

**Task IDs**: TASK-RPT-001 through TASK-RPT-006

**Description**: Generate a structured report outline based on report type, available data, and required sections. Outline is validated before full generation to prevent wasted LLM calls.

**Input**:
```json
{
  "report_type": "string",
  "unified_context": {},
  "required_sections": ["array of section names (optional override)"],
  "tone": "enum[formal, executive, technical, conversational]",
  "max_word_count": 2000
}
```

**Output**:
```json
{
  "outline": [
    {
      "section_id": "string",
      "section_title": "string",
      "section_type": "enum[narrative, table, chart, list, metric_callout]",
      "data_sources_required": ["array of source_ids"],
      "estimated_word_count": 0,
      "is_required": true
    }
  ],
  "total_estimated_word_count": 0,
  "outline_warnings": ["array of sections with insufficient data"]
}
```

---

### Step 3: Section-by-Section Generation (`STEP-RPT-03`)

**Task IDs**: TASK-RPT-001 through TASK-RPT-006

**Description**: Generate each report section independently using constrained LLM prompts. Each section references only its designated data sources — preventing cross-contamination and enabling citation tracking.

**Input** (per section):
```json
{
  "section_id": "string",
  "section_title": "string",
  "section_type": "string",
  "section_data": {},
  "tone": "string",
  "max_words": 0,
  "citation_registry": {},
  "previous_sections_summary": "string (for narrative coherence)"
}
```

**Output** (per section):
```json
{
  "section_id": "string",
  "content": "string (formatted content in target markup)",
  "citations_used": ["array of source_ids"],
  "word_count": 0,
  "generation_confidence": 0.0,
  "hallucination_flags": []
}
```

**Hallucination Prevention Protocol**:
1. All numeric claims must be traceable to `section_data`
2. LLM is prompted in "grounded generation" mode: "Only state facts present in the provided data"
3. Post-generation: each numeric/named entity is verified against source data
4. Unverifiable claims are flagged in `hallucination_flags`

---

### Step 4: Hallucination Detection (`STEP-RPT-04`)

**Task IDs**: TASK-RPT-002, TASK-RPT-004

**Description**: Cross-reference all generated claims against source data. Assign hallucination risk score per section.

**Input**:
```json
{
  "generated_sections": [],
  "unified_context": {},
  "detection_config": {
    "numeric_tolerance": 0.001,
    "entity_matching_threshold": 0.90,
    "flag_unverifiable_claims": true
  }
}
```

**Output**:
```json
{
  "hallucination_report": {
    "overall_hallucination_rate": 0.0,
    "flagged_claims": [
      {
        "section_id": "string",
        "claim_text": "string",
        "claim_type": "enum[numeric, entity, causal, comparative]",
        "source_data": "any",
        "is_grounded": false,
        "confidence": 0.0
      }
    ],
    "clean_sections": ["array of section_ids with zero flags"],
    "requires_human_review": false
  }
}
```

**Metric Target**: `hallucination_rate ≤ 0.03` (spec threshold from `docs/spec.md#81`)

---

### Step 5: Visualization Generation (`STEP-RPT-05`)

**Task IDs**: TASK-RPT-003, TASK-RPT-005

**Description**: Generate charts and visualizations from tabular data sources. Output as base64-encoded PNG or SVG inline in report.

**Supported Chart Types**:

| Chart Type          | Best For                              | Library        |
|---------------------|---------------------------------------|----------------|
| `bar`               | Category comparisons                  | matplotlib     |
| `line`              | Time series, trends                   | matplotlib     |
| `pie`               | Composition / proportions             | matplotlib     |
| `heatmap`           | Correlation matrices                  | seaborn        |
| `waterfall`         | Financial reconciliation deltas       | plotly         |
| `sankey`            | PO-to-invoice flow                    | plotly         |
| `metric_card`       | KPI callout (single number)           | HTML/CSS       |

**Input**:
```json
{
  "visualizations": [
    {
      "chart_id": "string",
      "chart_type": "string",
      "title": "string",
      "data": {"labels": [], "values": [], "series": []},
      "config": {"color_scheme": "enterprise_blue", "show_legend": true}
    }
  ],
  "output_format": "enum[png_base64, svg_inline, plotly_json]"
}
```

**Output**:
```json
{
  "visualization_artifacts": [
    {
      "chart_id": "string",
      "artifact_data": "string (base64 or SVG or JSON)",
      "artifact_type": "string",
      "dimensions": {"width_px": 0, "height_px": 0}
    }
  ]
}
```

---

### Step 6: Document Assembly & Formatting (`STEP-RPT-06`)

**Task IDs**: TASK-RPT-001 through TASK-RPT-006

**Description**: Assemble all sections and visualizations into final formatted document. Apply template, branding, table of contents, and citation appendix.

**Input**:
```json
{
  "report_type": "string",
  "generated_sections": [],
  "visualization_artifacts": [],
  "citation_registry": {},
  "output_format": "enum[markdown, pdf, html, docx]",
  "metadata": {
    "report_title": "string",
    "author": "string",
    "date": "YYYY-MM-DD",
    "classification": "enum[internal, confidential, public]",
    "version": "string"
  }
}
```

**Output**:
```json
{
  "report_content": "string (full formatted document)",
  "report_artifact_path": "string (file path)",
  "table_of_contents": [],
  "citations": [
    {
      "citation_id": "string",
      "source_id": "string",
      "citation_text": "string",
      "url": "string (optional)"
    }
  ],
  "word_count": 0,
  "page_count": 0,
  "generation_metadata": {
    "model": "string",
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "generation_time_ms": 0,
    "cost_usd": 0.0
  }
}
```

---

## Citation Standard

All reports must cite data sources using the following inline format:

- **Markdown**: `[^source_id]` with footnote at end of section
- **HTML**: `<cite data-source-id="source_id">` tags
- **PDF/DOCX**: Footnote numbering with appendix

Citation appendix format:
```
[1] FinancialReconAgent Output — Run ID: abc-123 — 2026-11-05T14:22:00Z
[2] Invoice Dataset: Q4-2026 AP Export — Acme Corp ERP — accessed 2026-11-05
```

---

## Tone Guidelines

| Tone          | Characteristics                                               | Avoid                              |
|---------------|---------------------------------------------------------------|------------------------------------|
| `executive`   | Concise, action-oriented, metric-first, no jargon            | Technical details, raw data dumps  |
| `formal`      | Professional, passive voice acceptable, citations prominent   | Casual language, contractions      |
| `technical`   | Precise, definitions included, methodology visible            | Vague summaries, unsupported claims|

---

## Configuration

```yaml
# skills/config/report_generation.yaml
report_generation:
  llm_model: gemini-2.5-pro
  llm_temperature: 0.3
  hallucination_detection_enabled: true
  hallucination_rate_threshold: 0.03
  max_concurrent_sections: 4
  default_tone: executive
  citation_format: footnote
  visualization_library: plotly
  color_scheme: enterprise_blue
  pdf_engine: weasyprint
  docx_template: templates/enterprise_report.docx
  enable_toc: true
  word_count_limits:
    executive_summary: 800
    audit_report: 5000
    operational_dashboard: 500
    financial_statement: 3000
```

---

## Error Codes

| Code      | Description                                       | Retry? |
|-----------|---------------------------------------------------|--------|
| `RPT-001` | Insufficient data for required section            | No     |
| `RPT-002` | Hallucination rate exceeds threshold              | No     |
| `RPT-003` | PDF rendering engine failure                      | Yes    |
| `RPT-004` | Visualization generation failure                  | Yes    |
| `RPT-005` | Word count exceeded maximum limit                 | No     |
| `RPT-006` | Citation registry empty (no grounded sources)     | No     |

---

## Test Coverage Requirements

- [ ] `TC-RPT-001`: Executive summary — ≤ 800 words, all KPIs present, zero hallucination flags
- [ ] `TC-RPT-002`: Audit report — SOX-compliant citations, all discrepancies documented
- [ ] `TC-RPT-003`: Dashboard — Waterfall chart for reconciliation delta generated correctly
- [ ] `TC-RPT-004`: Hallucination detection — Planted false claim caught with rate ≤ 0.03
- [ ] `TC-RPT-005`: Multi-format output — Same content in Markdown, PDF, HTML are equivalent
- [ ] `TC-RPT-006`: Idempotency — Same input, same word count, same citations
