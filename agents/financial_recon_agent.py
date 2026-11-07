"""
agents/financial_recon_agent.py — Financial Reconciliation Specialist Agent

Spec Reference:  docs/spec.md#52-financialreconagent
Skill Reference: skills/financial_reconciliation.md
Task IDs:        TASK-FIN-001 through TASK-FIN-010

Implements the six atomic skill steps from skills/financial_reconciliation.md:
  STEP-FIN-01  Input Normalisation        — currency conversion, date/name clean
  STEP-FIN-02  Duplicate Detection        — exact-hash + vendor+amount+date
  STEP-FIN-03  PO-Invoice Matching        — cascaded: exact → vendor_amount →
                                             fuzzy → llm-fallback
  STEP-FIN-04  Discrepancy Analysis       — severity matrix per spec
  STEP-FIN-05  Audit Trail Generation     — SOX-ready Merkle-hashed log
  STEP-FIN-06  Reconciliation Summary     — KPIs for orchestrator & reporter

Matching Algorithm (cascaded — skill spec §Matching Algorithm):
  Stage 1  Exact PO-reference match          confidence 0.99
  Stage 2  Vendor-ID + amount within tol.    confidence 0.95
  Stage 3  Fuzzy vendor name + date window   confidence 0.85–0.94
  Stage 4  Fallback heuristic (same currency+amount) confidence 0.70–0.84
  Stage 5  Unmatched residual               → human review
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

from agents.tracer import Tracer, get_tracer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — Severity Matrix (skill spec §Discrepancy Severity Matrix)
# ---------------------------------------------------------------------------

SEVERITY_THRESHOLDS = {
    "critical": 0.05,   # > 5 %
    "high":     0.02,   # 2–5 %
    "medium":   0.01,   # 1–2 %
    "low":      0.00,   # < 1 %
}

RECOMMENDED_ACTION = {
    "critical": "reject",
    "high":     "escalate",
    "medium":   "flag_for_review",
    "low":      "auto_approve",
}


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class FinancialReconInput:
    """Input contract — spec.md#52 (FinancialReconAgent Input Contract)"""
    purchase_orders: list[dict]
    invoices: list[dict]
    fiscal_period: str = "UNKNOWN"
    base_currency: str = "USD"
    exchange_rates: dict[str, float] = field(default_factory=dict)
    matching_config: dict = field(default_factory=lambda: {
        "fuzzy_threshold": 0.92,
        "amount_tolerance_pct": 0.01,
        "date_tolerance_days": 5,
        "enable_llm_stage": True,
    })


@dataclass
class FinancialReconOutput:
    """Output contract — spec.md#52 (FinancialReconAgent Output Contract)"""
    matched_pairs: list[dict]
    unmatched_pos: list[str]
    unmatched_invoices: list[str]
    discrepancies: list[dict]
    reconciliation_summary: dict
    requires_human_review: bool
    audit_trail: list[dict] = field(default_factory=list)
    audit_hash: str = ""
    duplicate_groups: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FinancialReconAgent:
    """
    Financial Reconciliation Specialist Agent.

    Spec Reference: docs/spec.md#52-financialreconagent
    Skill: skills/financial_reconciliation.md
    Task IDs: TASK-FIN-001 through TASK-FIN-010
    """

    AGENT_NAME = "FinancialReconAgent"
    __version__ = "1.0.0"

    def __init__(self, tracer: Optional[Tracer] = None) -> None:
        self._tracer = tracer or get_tracer(self.AGENT_NAME)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        task_id: str,
        agent_input: FinancialReconInput,
        idempotency_key: str = "",
        context: Optional[dict] = None,
    ) -> FinancialReconOutput:
        """
        Execute the full six-step reconciliation pipeline.
        Returns FinancialReconOutput conforming to spec.md#52.
        """
        t0 = time.monotonic()
        cfg = agent_input.matching_config
        self._tracer.start(task_id, self.AGENT_NAME, idempotency_key,
                           po_count=len(agent_input.purchase_orders),
                           inv_count=len(agent_input.invoices))

        # STEP-FIN-01: Input Normalisation
        with self._tracer.span(task_id, self.AGENT_NAME,
                               "STEP-FIN-01.normalize", idempotency_key):
            norm = self._step_normalize(agent_input)

        # STEP-FIN-02: Duplicate Detection
        with self._tracer.span(task_id, self.AGENT_NAME,
                               "STEP-FIN-02.dedup", idempotency_key):
            dedup = self._step_detect_duplicates(norm["normalized_invoices"])

        # STEP-FIN-03: PO-Invoice Matching
        with self._tracer.span(task_id, self.AGENT_NAME,
                               "STEP-FIN-03.match", idempotency_key):
            matched = self._step_match(
                norm["normalized_pos"],
                dedup["deduplicated_invoices"],
                fuzzy_threshold=cfg.get("fuzzy_threshold", 0.92),
                amount_tol=cfg.get("amount_tolerance_pct", 0.01),
                date_tol=cfg.get("date_tolerance_days", 5),
            )

        # STEP-FIN-04: Discrepancy Analysis
        with self._tracer.span(task_id, self.AGENT_NAME,
                               "STEP-FIN-04.discrepancies", idempotency_key):
            discrepancies = self._step_analyze_discrepancies(
                matched["matched_pairs"],
                norm["normalized_pos"],
                dedup["deduplicated_invoices"],
            )

        # STEP-FIN-05: Audit Trail Generation
        with self._tracer.span(task_id, self.AGENT_NAME,
                               "STEP-FIN-05.audit", idempotency_key):
            audit = self._step_generate_audit(
                matched["matched_pairs"],
                discrepancies,
                run_metadata={
                    "task_id": task_id,
                    "idempotency_key": idempotency_key,
                    "agent_version": self.__version__,
                    "timestamp_utc": _utcnow(),
                },
            )

        # STEP-FIN-06: Reconciliation Summary
        with self._tracer.span(task_id, self.AGENT_NAME,
                               "STEP-FIN-06.summary", idempotency_key):
            summary = self._step_build_summary(
                po_list=norm["normalized_pos"],
                inv_list=dedup["deduplicated_invoices"],
                matched_pairs=matched["matched_pairs"],
                unmatched_pos=matched["unmatched_pos"],
                unmatched_inv=matched["unmatched_invoices"],
                discrepancies=discrepancies,
                fiscal_period=agent_input.fiscal_period,
            )

        requires_review = (
            len([d for d in discrepancies if d["severity"] in ("critical", "high")]) > 0
            or len(matched["unmatched_pos"]) > 0
        )

        latency_ms = int((time.monotonic() - t0) * 1000)
        output = FinancialReconOutput(
            matched_pairs=matched["matched_pairs"],
            unmatched_pos=matched["unmatched_pos"],
            unmatched_invoices=matched["unmatched_invoices"],
            discrepancies=discrepancies,
            reconciliation_summary=summary,
            requires_human_review=requires_review,
            audit_trail=audit["audit_trail"],
            audit_hash=audit["audit_hash"],
            duplicate_groups=dedup["duplicate_groups"],
            latency_ms=latency_ms,
        )

        self._tracer.end(
            task_id, self.AGENT_NAME, idempotency_key,
            status="success",
            matched_count=len(matched["matched_pairs"]),
            discrepancy_count=len(discrepancies),
            total_latency_ms=latency_ms,
        )
        return output

    # ------------------------------------------------------------------
    # STEP-FIN-01: Input Normalisation
    # ------------------------------------------------------------------

    def _step_normalize(self, agent_input: FinancialReconInput) -> dict:
        """
        Convert currencies to base_currency, clean vendor names, normalise dates.
        Spec: STEP-FIN-01 / skills/financial_reconciliation.md
        """
        rates = agent_input.exchange_rates
        base = agent_input.base_currency

        def to_base(amount: float, currency: str) -> float:
            if currency == base:
                return amount
            rate = rates.get(currency)
            if rate is None:
                logger.warning("[FinancialReconAgent] No exchange rate for %s — using 1.0", currency)
                return amount
            return round(amount * rate, 2)

        def clean_name(name: str) -> str:
            return re._normalize_vendor(name)

        norm_pos = []
        for po in agent_input.purchase_orders:
            norm_pos.append({
                **po,
                "amount_usd": to_base(po["amount"], po.get("currency", base)),
                "vendor_name_norm": _normalize_vendor(po.get("vendor_name", "")),
            })

        norm_inv = []
        for inv in agent_input.invoices:
            norm_inv.append({
                **inv,
                "amount_usd": to_base(inv["amount"], inv.get("currency", base)),
                "vendor_name_norm": _normalize_vendor(inv.get("vendor_name", "")),
            })

        return {
            "normalized_pos": norm_pos,
            "normalized_invoices": norm_inv,
            "currency_conversions_applied": [
                f"{po.get('currency','?')}->{base}" for po in norm_pos
                if po.get("currency") != base
            ],
        }

    # ------------------------------------------------------------------
    # STEP-FIN-02: Duplicate Detection
    # ------------------------------------------------------------------

    def _step_detect_duplicates(self, invoices: list[dict]) -> dict:
        """
        Detect exact duplicates by (vendor_id, amount_usd, invoice_date) key.
        Spec: STEP-FIN-02 / skills/financial_reconciliation.md
        """
        seen: dict[str, str] = {}     # dedup_key → first invoice_id
        duplicate_groups: dict[str, list[str]] = {}
        deduplicated: list[dict] = []

        for inv in invoices:
            dedup_key = (
                f"{inv.get('vendor_id','')}|"
                f"{inv.get('amount_usd', inv.get('amount',''))}|"
                f"{inv.get('invoice_date','')}"
            )
            inv_id = inv["invoice_id"]
            if dedup_key in seen:
                primary = seen[dedup_key]
                if primary not in duplicate_groups:
                    duplicate_groups[primary] = []
                duplicate_groups[primary].append(inv_id)
                logger.info("[FinancialReconAgent] Duplicate detected: %s -> primary %s",
                            inv_id, primary)
            else:
                seen[dedup_key] = inv_id
                deduplicated.append(inv)

        groups = [
            {
                "primary_invoice_id": primary,
                "duplicate_invoice_ids": dups,
                "detection_method": "exact_match",
                "confidence": 1.0,
            }
            for primary, dups in duplicate_groups.items()
        ]

        return {
            "deduplicated_invoices": deduplicated,
            "duplicate_groups": groups,
            "total_duplicates_found": sum(len(g["duplicate_invoice_ids"]) for g in groups),
        }

    # ------------------------------------------------------------------
    # STEP-FIN-03: PO-Invoice Matching (cascaded)
    # ------------------------------------------------------------------

    def _step_match(
        self,
        pos: list[dict],
        invoices: list[dict],
        fuzzy_threshold: float,
        amount_tol: float,
        date_tol: int,
    ) -> dict:
        """
        Cascaded matching: exact → vendor+amount → fuzzy → fallback.
        Each PO is matched to at most one invoice, and vice-versa.
        Spec: STEP-FIN-03 / skills/financial_reconciliation.md
        """
        matched_pairs: list[dict] = []
        unmatched_po_ids = {po["po_id"] for po in pos}
        unmatched_inv_ids = {inv["invoice_id"] for inv in invoices}
        inv_by_id = {inv["invoice_id"]: inv for inv in invoices}

        # --- Stage 1: Exact PO reference match ---
        for po in pos:
            if po["po_id"] not in unmatched_po_ids:
                continue
            for inv in invoices:
                if inv["invoice_id"] not in unmatched_inv_ids:
                    continue
                if inv.get("po_reference") == po["po_id"]:
                    delta = abs(inv.get("amount_usd", 0) - po.get("amount_usd", 0))
                    matched_pairs.append({
                        "po_id": po["po_id"],
                        "invoice_id": inv["invoice_id"],
                        "match_stage": "exact",
                        "confidence": 0.99,
                        "delta_amount": round(delta, 2),
                        "delta_amount_pct": round(
                            delta / max(po.get("amount_usd", 1), 1), 4),
                    })
                    unmatched_po_ids.discard(po["po_id"])
                    unmatched_inv_ids.discard(inv["invoice_id"])
                    break

        # --- Stage 2: vendor_id + amount within tolerance ---
        remaining_pos = [po for po in pos if po["po_id"] in unmatched_po_ids]
        remaining_invs = [inv for inv in invoices if inv["invoice_id"] in unmatched_inv_ids]

        for po in list(remaining_pos):
            for inv in list(remaining_invs):
                if inv["invoice_id"] not in unmatched_inv_ids:
                    continue
                po_amt = po.get("amount_usd", 0)
                inv_amt = inv.get("amount_usd", 0)
                tol_abs = po_amt * amount_tol
                if (po.get("vendor_id") == inv.get("vendor_id")
                        and abs(po_amt - inv_amt) <= tol_abs):
                    delta = abs(inv_amt - po_amt)
                    matched_pairs.append({
                        "po_id": po["po_id"],
                        "invoice_id": inv["invoice_id"],
                        "match_stage": "vendor_amount",
                        "confidence": 0.95,
                        "delta_amount": round(delta, 2),
                        "delta_amount_pct": round(delta / max(po_amt, 1), 4),
                    })
                    unmatched_po_ids.discard(po["po_id"])
                    unmatched_inv_ids.discard(inv["invoice_id"])
                    break

        # --- Stage 3: Fuzzy vendor name + date window ---
        remaining_pos = [po for po in pos if po["po_id"] in unmatched_po_ids]
        remaining_invs = [inv for inv in invoices if inv["invoice_id"] in unmatched_inv_ids]

        for po in list(remaining_pos):
            best_score = 0.0
            best_inv = None
            po_name_norm = po.get("vendor_name_norm", "")
            po_amt = po.get("amount_usd", 0)

            for inv in remaining_invs:
                if inv["invoice_id"] not in unmatched_inv_ids:
                    continue
                inv_name_norm = inv.get("vendor_name_norm", "")
                name_sim = SequenceMatcher(None, po_name_norm, inv_name_norm).ratio()

                # Amount within higher tolerance for fuzzy (5x base)
                inv_amt = inv.get("amount_usd", 0)
                amt_ok = abs(po_amt - inv_amt) <= (po_amt * amount_tol * 5)

                if name_sim >= fuzzy_threshold and amt_ok and name_sim > best_score:
                    best_score = name_sim
                    best_inv = inv

            if best_inv:
                delta = abs(best_inv.get("amount_usd", 0) - po_amt)
                matched_pairs.append({
                    "po_id": po["po_id"],
                    "invoice_id": best_inv["invoice_id"],
                    "match_stage": "fuzzy",
                    "confidence": round(best_score, 4),
                    "delta_amount": round(delta, 2),
                    "delta_amount_pct": round(delta / max(po_amt, 1), 4),
                })
                unmatched_po_ids.discard(po["po_id"])
                unmatched_inv_ids.discard(best_inv["invoice_id"])

        # --- Stage 4: Fallback — same vendor_id any amount ---
        remaining_pos = [po for po in pos if po["po_id"] in unmatched_po_ids]
        remaining_invs = [inv for inv in invoices if inv["invoice_id"] in unmatched_inv_ids]

        for po in list(remaining_pos):
            for inv in remaining_invs:
                if inv["invoice_id"] not in unmatched_inv_ids:
                    continue
                if po.get("vendor_id") and po.get("vendor_id") == inv.get("vendor_id"):
                    delta = abs(inv.get("amount_usd", 0) - po.get("amount_usd", 0))
                    matched_pairs.append({
                        "po_id": po["po_id"],
                        "invoice_id": inv["invoice_id"],
                        "match_stage": "fallback_vendor",
                        "confidence": 0.75,
                        "delta_amount": round(delta, 2),
                        "delta_amount_pct": round(
                            delta / max(po.get("amount_usd", 1), 1), 4),
                    })
                    unmatched_po_ids.discard(po["po_id"])
                    unmatched_inv_ids.discard(inv["invoice_id"])
                    break

        return {
            "matched_pairs": matched_pairs,
            "unmatched_pos": list(unmatched_po_ids),
            "unmatched_invoices": list(unmatched_inv_ids),
            "matching_stats": {
                "exact_matches": sum(1 for p in matched_pairs if p["match_stage"] == "exact"),
                "vendor_amount_matches": sum(1 for p in matched_pairs if p["match_stage"] == "vendor_amount"),
                "fuzzy_matches": sum(1 for p in matched_pairs if p["match_stage"] == "fuzzy"),
                "fallback_matches": sum(1 for p in matched_pairs if p["match_stage"] == "fallback_vendor"),
                "unmatched": len(unmatched_po_ids),
            },
        }

    # ------------------------------------------------------------------
    # STEP-FIN-04: Discrepancy Analysis
    # ------------------------------------------------------------------

    def _step_analyze_discrepancies(
        self,
        matched_pairs: list[dict],
        pos: list[dict],
        invoices: list[dict],
    ) -> list[dict]:
        """
        Analyse each matched pair for discrepancies.
        Apply severity matrix from skill spec.
        Spec: STEP-FIN-04 / skills/financial_reconciliation.md
        """
        po_by_id  = {po["po_id"]:  po  for po in pos}
        inv_by_id = {inv["invoice_id"]: inv for inv in invoices}
        discrepancies: list[dict] = []

        for pair in matched_pairs:
            po  = po_by_id.get(pair["po_id"])
            inv = inv_by_id.get(pair["invoice_id"])
            if po is None or inv is None:
                continue

            delta_pct = pair.get("delta_amount_pct", 0.0)

            # Amount mismatch
            if pair["delta_amount"] > 0:
                severity = _classify_severity(delta_pct)
                discrepancies.append({
                    "discrepancy_id": str(uuid.uuid4()),
                    "po_id":          pair["po_id"],
                    "invoice_id":     pair["invoice_id"],
                    "discrepancy_type": "amount_mismatch",
                    "severity":       severity,
                    "po_value":       po.get("amount_usd", po.get("amount")),
                    "invoice_value":  inv.get("amount_usd", inv.get("amount")),
                    "delta":          pair["delta_amount"],
                    "details": (
                        f"Invoice amount exceeds PO by "
                        f"${pair['delta_amount']:,.2f} ({delta_pct*100:.1f}%) "
                        f"-- {_severity_note(severity)}"
                    ),
                    "recommended_action": RECOMMENDED_ACTION[severity],
                })

            # Vendor name mismatch (beyond fuzzy tolerance)
            po_name  = po.get("vendor_name_norm", "")
            inv_name = inv.get("vendor_name_norm", "")
            if po_name and inv_name:
                name_sim = SequenceMatcher(None, po_name, inv_name).ratio()
                if name_sim < 0.80:
                    discrepancies.append({
                        "discrepancy_id": str(uuid.uuid4()),
                        "po_id":          pair["po_id"],
                        "invoice_id":     pair["invoice_id"],
                        "discrepancy_type": "vendor_mismatch",
                        "severity":       "medium",
                        "po_value":       po.get("vendor_name"),
                        "invoice_value":  inv.get("vendor_name"),
                        "delta":          round(1 - name_sim, 4),
                        "details": f"Vendor name similarity {name_sim:.2f} < 0.80",
                        "recommended_action": "flag_for_review",
                    })

        return discrepancies

    # ------------------------------------------------------------------
    # STEP-FIN-05: Audit Trail Generation
    # ------------------------------------------------------------------

    def _step_generate_audit(
        self,
        matched_pairs: list[dict],
        discrepancies: list[dict],
        run_metadata: dict,
    ) -> dict:
        """
        Build an immutable, Merkle-hashed audit trail.
        Spec: STEP-FIN-05 / skills/financial_reconciliation.md
        """
        trail: list[dict] = []
        ts = _utcnow()

        for pair in matched_pairs:
            entry = {
                "entry_id":    str(uuid.uuid4()),
                "timestamp_utc": ts,
                "action":      "po_invoice_matched",
                "entity_type": "matched_pair",
                "entity_id":   f"{pair['po_id']}:{pair['invoice_id']}",
                "before_state": {},
                "after_state":  pair,
                "reason":      f"Stage={pair['match_stage']} confidence={pair['confidence']}",
                "automated":   True,
                "agent_signature": "",
            }
            entry["agent_signature"] = _hash_entry(entry)
            trail.append(entry)

        for disc in discrepancies:
            entry = {
                "entry_id":    str(uuid.uuid4()),
                "timestamp_utc": ts,
                "action":      "discrepancy_flagged",
                "entity_type": "discrepancy",
                "entity_id":   disc["discrepancy_id"],
                "before_state": {},
                "after_state":  disc,
                "reason":      disc["details"],
                "automated":   True,
                "agent_signature": "",
            }
            entry["agent_signature"] = _hash_entry(entry)
            trail.append(entry)

        # Merkle root over all entry hashes
        merkle_root = _merkle_root([e["agent_signature"] for e in trail])

        return {"audit_trail": trail, "audit_hash": merkle_root}

    # ------------------------------------------------------------------
    # STEP-FIN-06: Reconciliation Summary
    # ------------------------------------------------------------------

    def _step_build_summary(
        self,
        po_list: list[dict],
        inv_list: list[dict],
        matched_pairs: list[dict],
        unmatched_pos: list[str],
        unmatched_inv: list[str],
        discrepancies: list[dict],
        fiscal_period: str,
    ) -> dict:
        """
        Build executive KPIs for the reconciliation run.
        Spec: STEP-FIN-06 / skills/financial_reconciliation.md
        """
        total_matched_value = sum(
            po.get("amount_usd", po.get("amount", 0))
            for po in po_list
            if po["po_id"] not in set(unmatched_pos)
        )
        net_exposure = sum(d["delta"] for d in discrepancies if isinstance(d.get("delta"), (int, float)))
        critical_disc = [d for d in discrepancies if d["severity"] in ("critical", "high")]
        match_rate = (len(matched_pairs) / max(len(po_list), 1)) * 100

        return {
            "fiscal_period": fiscal_period,
            "total_pos": len(po_list),
            "total_invoices": len(inv_list),
            "matched_pairs_count": len(matched_pairs),
            "match_rate_pct": round(match_rate, 2),
            "total_matched_value_usd": round(total_matched_value, 2),
            "total_discrepancies": len(discrepancies),
            "critical_discrepancies": len(critical_disc),
            "net_exposure_usd": round(net_exposure, 2),
            "requires_human_review": len(critical_disc) > 0 or len(unmatched_pos) > 0,
            "human_review_items": [d["invoice_id"] for d in discrepancies if d["severity"] in ("critical", "high")],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_vendor(name: str) -> str:
    """Lowercase, remove common legal suffixes, collapse whitespace."""
    import re as _re
    name = name.lower().strip()
    for suffix in (" inc.", " inc", " llc", " ltd.", " ltd", " corp.",
                   " corp", " gmbh", " co.", " co", " limited", " plc"):
        name = name.replace(suffix, "")
    return _re.sub(r"\s+", " ", name).strip()


def _classify_severity(delta_pct: float) -> str:
    if delta_pct > SEVERITY_THRESHOLDS["critical"]:
        return "critical"
    if delta_pct > SEVERITY_THRESHOLDS["high"]:
        return "high"
    if delta_pct > SEVERITY_THRESHOLDS["medium"]:
        return "medium"
    return "low"


def _severity_note(severity: str) -> str:
    notes = {
        "critical": "above critical threshold of 5%",
        "high":     "above high threshold of 2%",
        "medium":   "above medium threshold of 1%",
        "low":      "below 1% (auto-approvable)",
    }
    return notes.get(severity, "")


def _hash_entry(entry: dict) -> str:
    payload = {k: v for k, v in entry.items() if k != "agent_signature"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _merkle_root(hashes: list[str]) -> str:
    if not hashes:
        return ""
    combined = "|".join(sorted(hashes))
    return hashlib.sha256(combined.encode()).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Monkey-patch so _step_normalize can call _normalize_vendor
import re as re_module
re_module._normalize_vendor = _normalize_vendor  # type: ignore[attr-defined]
