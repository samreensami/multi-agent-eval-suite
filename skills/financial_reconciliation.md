---
skill_id: financial_reconciliation
version: 1.0.0
spec_ref: docs/spec.md#52-financialreconagent
task_ids: [TASK-FIN-001, TASK-FIN-002, TASK-FIN-003, TASK-FIN-004, TASK-FIN-005, TASK-FIN-006, TASK-FIN-007, TASK-FIN-008, TASK-FIN-009, TASK-FIN-010]
status: APPROVED
owner: FinancialReconAgent
dependencies: [data_extraction]
---

# Skill: Financial Reconciliation

## Purpose

Provides atomic, auditable financial reconciliation capabilities including purchase order-to-invoice matching, multi-currency normalization, discrepancy detection, and anomaly flagging. Designed to replicate and benchmark against experienced AP (Accounts Payable) analyst FTE workflows.

---

## Business Context

| FTE Process                       | Skill Coverage     | Automation Level |
|-----------------------------------|--------------------|-----------------|
| PO-to-Invoice Matching            | Full               | 95%             |
| Discrepancy Detection             | Full               | 100%            |
| Multi-Currency Conversion         | Full               | 100%            |
| Duplicate Invoice Detection       | Full               | 100%            |
| Vendor Validation                 | Partial            | 70%             |
| Anomaly Escalation                | Full (flag only)   | 100% flag, 0% resolve |

---

## Matching Algorithm

The reconciliation engine uses a **multi-stage cascaded matching** approach:

```
Stage 1: Exact Match
  → PO Number = Invoice PO Reference   [confidence: 0.99]

Stage 2: Vendor + Amount Match
  → vendor_id match AND |po_amount - inv_amount| / po_amount ≤ tolerance   [confidence: 0.95]

Stage 3: Fuzzy Vendor + Date Window Match
  → fuzzy_match(vendor_name) ≥ threshold AND date within ±date_tolerance_days   [confidence: 0.85-0.94]

Stage 4: LLM-Assisted Semantic Match
  → LLM reasons over remaining unmatched pairs   [confidence: 0.70-0.84]

Stage 5: Unmatched Residual
  → Route to human review queue   [confidence: N/A]
```

---

## Skill Steps (Atomic Operations)

### Step 1: Input Normalization (`STEP-FIN-01`)

**Task IDs**: TASK-FIN-001

**Description**: Normalize PO and invoice records to canonical schema. Convert currencies, standardize dates, clean vendor names.

**Input**:
```json
{
  "purchase_orders": [
    {
      "po_id": "string",
      "amount": 0.0,
      "currency": "ISO 4217 code",
      "vendor_id": "string",
      "vendor_name": "string",
      "po_date": "YYYY-MM-DD",
      "line_items": [
        {"description": "string", "quantity": 0, "unit_price": 0.0, "subtotal": 0.0}
      ]
    }
  ],
  "invoices": [
    {
      "invoice_id": "string",
      "amount": 0.0,
      "currency": "ISO 4217 code",
      "vendor_id": "string",
      "vendor_name": "string",
      "invoice_date": "YYYY-MM-DD",
      "po_reference": "string (optional)",
      "line_items": []
    }
  ],
  "base_currency": "USD",
  "exchange_rates": {"EUR": 1.08, "GBP": 1.27}
}
```

**Output**:
```json
{
  "normalized_pos": [],
  "normalized_invoices": [],
  "currency_conversions_applied": ["array of conversion descriptions"],
  "normalization_warnings": ["array of data quality warnings"]
}
```

---

### Step 2: Duplicate Detection (`STEP-FIN-02`)

**Task IDs**: TASK-FIN-002

**Description**: Detect duplicate invoices before matching. Duplicates are defined as invoices with identical (vendor_id, amount, invoice_date) or (invoice_number within same vendor).

**Input**:
```json
{
  "normalized_invoices": []
}
```

**Output**:
```json
{
  "duplicate_groups": [
    {
      "primary_invoice_id": "string",
      "duplicate_invoice_ids": ["array"],
      "detection_method": "string",
      "confidence": 0.0
    }
  ],
  "deduplicated_invoices": [],
  "total_duplicates_found": 0
}
```

---

### Step 3: PO-Invoice Matching (`STEP-FIN-03`)

**Task IDs**: TASK-FIN-003, TASK-FIN-004, TASK-FIN-005

**Description**: Execute multi-stage cascaded matching algorithm. Produce matched pairs with confidence scores.

**Input**:
```json
{
  "normalized_pos": [],
  "deduplicated_invoices": [],
  "matching_config": {
    "fuzzy_threshold": 0.92,
    "amount_tolerance_pct": 0.01,
    "date_tolerance_days": 5,
    "enable_llm_stage": true,
    "llm_max_candidates": 50
  }
}
```

**Output**:
```json
{
  "matched_pairs": [
    {
      "po_id": "string",
      "invoice_id": "string",
      "match_stage": "enum[exact, vendor_amount, fuzzy, llm]",
      "confidence": 0.0,
      "delta_amount": 0.0,
      "delta_amount_pct": 0.0
    }
  ],
  "unmatched_pos": ["array of po_ids"],
  "unmatched_invoices": ["array of invoice_ids"],
  "matching_stats": {
    "exact_matches": 0,
    "fuzzy_matches": 0,
    "llm_matches": 0,
    "unmatched": 0
  }
}
```

---

### Step 4: Discrepancy Analysis (`STEP-FIN-04`)

**Task IDs**: TASK-FIN-006, TASK-FIN-007, TASK-FIN-008

**Description**: For each matched pair, perform detailed discrepancy analysis across all relevant fields. Assign severity levels.

**Input**:
```json
{
  "matched_pairs": [],
  "normalized_pos": [],
  "deduplicated_invoices": [],
  "discrepancy_config": {
    "amount_critical_threshold_pct": 0.05,
    "amount_high_threshold_pct": 0.02,
    "amount_medium_threshold_pct": 0.01
  }
}
```

**Output**:
```json
{
  "discrepancies": [
    {
      "discrepancy_id": "string (uuid)",
      "po_id": "string",
      "invoice_id": "string",
      "discrepancy_type": "enum[amount_mismatch, vendor_mismatch, date_out_of_range, missing_line_item, quantity_mismatch, tax_discrepancy, duplicate_charge]",
      "severity": "enum[low, medium, high, critical]",
      "po_value": "any",
      "invoice_value": "any",
      "delta": "any",
      "details": "string",
      "recommended_action": "enum[auto_approve, flag_for_review, escalate, reject]"
    }
  ],
  "discrepancy_summary": {
    "total_discrepancies": 0,
    "by_severity": {"low": 0, "medium": 0, "high": 0, "critical": 0},
    "by_type": {},
    "net_financial_exposure_usd": 0.0
  }
}
```

---

### Step 5: Audit Trail Generation (`STEP-FIN-05`)

**Task IDs**: TASK-FIN-009

**Description**: Generate immutable audit trail for all reconciliation decisions. Compliant with SOX and GAAP audit requirements.

**Input**:
```json
{
  "matched_pairs": [],
  "discrepancies": [],
  "run_metadata": {
    "run_id": "string",
    "agent_version": "string",
    "skill_version": "string",
    "operator": "string",
    "timestamp_utc": "string"
  }
}
```

**Output**:
```json
{
  "audit_trail": [
    {
      "entry_id": "string (uuid)",
      "timestamp_utc": "string",
      "action": "string",
      "entity_type": "enum[po, invoice, matched_pair, discrepancy]",
      "entity_id": "string",
      "before_state": {},
      "after_state": {},
      "reason": "string",
      "automated": true,
      "agent_signature": "string (SHA256 of entry)"
    }
  ],
  "audit_hash": "string (Merkle root of all entry hashes)"
}
```

---

### Step 6: Reconciliation Summary (`STEP-FIN-06`)

**Task IDs**: TASK-FIN-010

**Description**: Produce executive reconciliation summary with KPIs, match rates, and human review queue.

**Input**:
```json
{
  "matched_pairs": [],
  "unmatched_pos": [],
  "unmatched_invoices": [],
  "discrepancies": [],
  "fiscal_period": "YYYY-QN"
}
```

**Output**:
```json
{
  "reconciliation_summary": {
    "fiscal_period": "string",
    "total_pos": 0,
    "total_invoices": 0,
    "matched_pairs_count": 0,
    "match_rate_pct": 0.0,
    "total_matched_value_usd": 0.0,
    "total_discrepancies": 0,
    "net_exposure_usd": 0.0,
    "requires_human_review": false,
    "human_review_items": []
  }
}
```

---

## Discrepancy Severity Matrix

| Discrepancy Type    | Low (<1%)     | Medium (1-2%)   | High (2-5%)    | Critical (>5%) |
|---------------------|---------------|-----------------|----------------|----------------|
| Amount Mismatch     | Auto-approve  | Flag review     | Escalate       | Reject + Alert |
| Date Out of Range   | ≤2 days: Auto | 3-5 days: Flag  | 6-14 days: Esc | >14 days: Reject|
| Vendor Mismatch     | —             | Flag review     | Escalate       | Reject + Alert |
| Duplicate Charge    | —             | —               | Reject         | Reject + Alert |
| Missing Line Item   | Auto-approve  | Flag review     | Escalate       | Reject         |

---

## Error Codes

| Code      | Description                                           | Retry? |
|-----------|-------------------------------------------------------|--------|
| `FIN-001` | Currency not in exchange rate table                   | No     |
| `FIN-002` | PO dataset empty                                      | No     |
| `FIN-003` | Invoice dataset empty                                 | No     |
| `FIN-004` | Fuzzy matching service timeout                        | Yes    |
| `FIN-005` | LLM matching stage failed                             | Yes    |
| `FIN-006` | Audit trail signing key unavailable                   | No     |
| `FIN-007` | Exchange rate data stale (> 24h)                      | Yes    |

---

## Configuration

```yaml
# skills/config/financial_reconciliation.yaml
financial_reconciliation:
  default_base_currency: USD
  fuzzy_threshold: 0.92
  amount_tolerance_pct: 0.01
  date_tolerance_days: 5
  enable_llm_stage: true
  llm_model: gemini-2.5-pro
  llm_temperature: 0.0  # Zero temperature for deterministic financial decisions
  max_records_per_run: 10000
  enable_duplicate_detection: true
  audit_trail_enabled: true
  sox_compliance_mode: true
  exchange_rate_provider: "ecb"  # European Central Bank
  exchange_rate_cache_ttl_hours: 24
  severity_thresholds:
    amount_critical_pct: 0.05
    amount_high_pct: 0.02
    amount_medium_pct: 0.01
```

---

## Test Coverage Requirements

- [ ] `TC-FIN-001`: 100 PO-invoice pairs — 95 exact matches, 5 fuzzy
- [ ] `TC-FIN-002`: Duplicate invoice detection — 3 duplicate sets
- [ ] `TC-FIN-003`: Multi-currency reconciliation (USD, EUR, GBP)
- [ ] `TC-FIN-004`: Amount discrepancy at critical threshold (5.1%)
- [ ] `TC-FIN-005`: Missing line item discrepancy detection
- [ ] `TC-FIN-006`: LLM-assisted matching for ambiguous vendor names
- [ ] `TC-FIN-007`: SOX audit trail completeness verification
- [ ] `TC-FIN-008`: Idempotency — identical input, identical audit hash
- [ ] `TC-FIN-009`: Stale exchange rate error handling
- [ ] `TC-FIN-010`: Large dataset performance (10,000 records ≤ 15s)
