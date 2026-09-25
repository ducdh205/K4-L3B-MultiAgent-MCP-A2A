"""L3B coordinator state machine, bounded by the public contracts.

MCP responses are the only source for evidence references.  When a specialist
cannot establish a fact, the verifier keeps the result as insufficient evidence
instead of inventing data to make a more attractive answer.
"""

from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .contracts import Contracts


class VerifierAgent:
    def __init__(self, trace: TraceWriter, case_id: str):
        self.trace = trace
        self.case_id = case_id
        # Sử dụng Contracts đã có sẵn trong thư mục schemas
        schemas_dir = Path(__file__).parent.parent.parent / "contracts" / "schemas"
        self.contracts = Contracts(schemas_dir)

    def _validate_schema(self, output: dict[str, Any], errors: List[str]) -> None:
        if output.get("case_id") != self.case_id:
            errors.append(f"case_id mismatch: expected {self.case_id}, got {output.get('case_id')}")
        try:
            # Lưu ý: Hàm này của repository sẽ raise lỗi đầu tiên nó gặp phải
            self.contracts.validate_output(output, label="verifier")
        except Exception as e:
            errors.append(f"Schema validation failed: {str(e)}")

    def _validate_evidence(self, output: dict[str, Any], collected_evidence_refs: Set[str], errors: List[str]) -> None:
        output_evidence = output.get("evidence_refs", [])
        if not output_evidence:
            errors.append("Output requires at least one valid evidence_ref.")
            
        fake_evidences = [ref for ref in output_evidence if ref not in collected_evidence_refs]
        if fake_evidences:
            errors.append(f"Found hallucinated or invalid evidence_refs: {fake_evidences}")

    def _validate_consistency(self, output: dict[str, Any], errors: List[str]) -> None:
        assessment = output.get("assessment", {})
        payment = output.get("payment_analysis", {})
        financial = output.get("financial_resolution", {})
        
        refunded = financial.get("total_refund_brl", 0)
        refundable = payment.get("refundable_total_brl", 0)
        if refunded and refundable is not None and refunded > refundable:
            errors.append(f"Refund total ({refunded}) exceeds refundable amount ({refundable})")

        if assessment.get("case_status") in ["action_required", "no_action"]:
            er_status = output.get("entity_resolution", {}).get("status")
            if er_status != "resolved":
                errors.append(f"Cannot close case when entity_resolution is '{er_status}'")

    def _validate_confidence(self, output: dict[str, Any], errors: List[str]) -> None:
        assessment = output.get("assessment", {})
        confidence = assessment.get("confidence", 1.0)
        conflicts = output.get("data_conflicts", [])
        
        if conflicts and confidence > 0.8:
            errors.append("Confidence is too high ( > 0.8) while there are unresolved data conflicts.")
            
        er_status = output.get("entity_resolution", {}).get("status")
        if er_status == "ambiguous" and confidence > 0.5:
            errors.append("Confidence is too high for an ambiguous entity resolution.")

    def verify(self, proposed_output: dict[str, Any], collected_evidence_refs: Set[str]) -> tuple[bool, str]:
        """
        Kiểm tra toàn diện output. Trả về True nếu pass, False và chuỗi lỗi nếu thất bại.
        Gom tất cả các lỗi lại để LLM Agent có thể sửa toàn bộ trong 1 lần retry.
        """
        errors: List[str] = []
        
        self._validate_schema(proposed_output, errors)
        self._validate_evidence(proposed_output, collected_evidence_refs, errors)
        self._validate_consistency(proposed_output, errors)
        self._validate_confidence(proposed_output, errors)
        
        if not errors:
            self.trace.emit(
                case_id=self.case_id,
                event_type="verification_completed",
                actor="verifier",
                status="success"
            )
            return True, "Verification passed."
        else:
            feedback_msg = " | ".join(errors)
            self.trace.emit(
                case_id=self.case_id,
                event_type="verification_failed",
                actor="verifier",
                reason=feedback_msg
            )
            return False, feedback_msg


def build_safe_fallback_output(case_id: str, collected_evidence_refs: Set[str]) -> dict[str, Any]:
    """Fallback output an toàn nếu không thể giải quyết case hợp lệ sau nhiều lần thử."""
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "unknown",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.0
        },
        "affected_entities": {"customers": [], "orders": [], "sellers": []},
        "claim_assessments": [],
        "entity_resolution": {
            "status": "not_found",
            "resolved_order_ids": [],
            "rejected_candidates": [],
            "confidence": 0.0
        },
        "customer_context": {
            "customer_unique_id": None,
            "related_order_ids": []
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None
        },
        "root_cause_analysis": {
            "reason": "unknown",
            "responsible_parties": []
        },
        "evidence_refs": list(collected_evidence_refs)[:1] if collected_evidence_refs else [],
        "data_conflicts": [],
        "financial_resolution": {
            "total_refund_brl": 0,
            "seller_deductions_brl": 0,
            "platform_loss_brl": 0
        },
        "resolution_actions": []
    }

_CATALOG: dict[int, tuple[str, ...]] = {}


def _tool(tools: Iterable[str], *aliases: str) -> str | None:
    """Choose a discovered tool; aliases avoid assuming one gateway naming style."""
    available = tuple(tools)
    for alias in aliases:
        if alias in available:
            return alias
    for name in available:
        normalized = name.lower().replace("-", "_")
        if any(alias in normalized for alias in aliases):
            return name
    return None


def _values(value: Any, *keys: str) -> list[str]:
    """Read actual string/number identifiers from arbitrarily nested MCP data."""
    wanted = set(keys)
    output: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in wanted and isinstance(item, (str, int)) and str(item):
                    output.append(str(item))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return list(dict.fromkeys(output))


def _amounts(value: Any, *keys: str) -> list[float]:
    wanted = set(keys)
    output: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in wanted and isinstance(item, (int, float)) and not isinstance(item, bool):
                    output.append(float(item))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return output


def _has_status(data: Any, *needles: str) -> bool:
    status = " ".join(_values(data, "status", "state", "verdict", "reason", "code")).lower()
    return any(needle in status for needle in needles)


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3B coordinator and specialist-agent workflow here.

    Include entity resolution, conflict handling and evidence-efficient investigation.
    The starter kit intentionally does not generate invented fallback answers.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3B multi-agent workflow in solve_case()")
