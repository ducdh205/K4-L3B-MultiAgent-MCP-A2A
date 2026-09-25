"""L3B coordinator state machine, bounded by the public contracts.

MCP responses are the only source for evidence references.  When a specialist
cannot establish a fact, the verifier keeps the result as insufficient evidence
instead of inventing data to make a more attractive answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

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
    """Run Coordinator → specialists → Policy → Verifier for exactly one case."""
    case_id = str(case["case_id"])
    tools = _CATALOG.get(id(gateway))
    if tools is None:
        tools = tuple(await gateway.list_tools())
        _CATALOG[id(gateway)] = tools

    records: list[dict[str, Any]] = []
    call_cache: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    async def collect(actor: str, tool_name: str | None, **arguments: str) -> dict[str, Any] | None:
        """Run one permitted MCP query and make its consumption observable."""
        if tool_name is None:
            return None
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in call_cache:
            return None
        call_cache.add(cache_key)
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        try:
            result = await gateway.call(tool_name, case_id=case_id, **arguments)
        except (RuntimeError, ValueError):
            trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor=actor,
                decision_code="tool_unavailable",
                tool_name=tool_name,
            )
            return None
        records.append(result)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[result["evidence_ref"]],
        )
        return result

    # Coordinator / entity resolution: bounded candidate lookup, never a broad scan.
    request = case.get("customer_request") if isinstance(case.get("customer_request"), dict) else {}
    candidates = [item for item in case.get("candidate_order_ids", []) if isinstance(item, str)][:5]
    claimed = request.get("claimed_order_id")
    if isinstance(claimed, str) and claimed not in candidates:
        candidates.insert(0, claimed)
    order_tool = _tool(tools, "get_order", "lookup_order", "resolve_order")
    order_results = [
        result
        for candidate in candidates
        if (result := await collect("order-item-agent", order_tool, order_id=candidate)) is not None
    ]
    resolved = list(
        dict.fromkeys(
            found
            for result in order_results
            for found in _values(result.get("data"), "order_id")
            if found in candidates
        )
    )
    # An exact request with one candidate is safely tied to its successful response.
    if not resolved and len(candidates) == len(order_results) == 1:
        resolved = candidates[:]
    resolution_status = (
        "resolved" if len(resolved) == 1 else "ambiguous" if resolved else "not_found"
    )
    order_id = resolved[0] if resolution_status == "resolved" else None
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="specialists",
        decision_code=resolution_status,
    )

    # Specialist branch. The three roles have separate permissions and inputs.
    item_tool = _tool(tools, "get_order_items", "get_items", "get_product")
    payment_tool = _tool(tools, "get_payment", "get_payments", "get_transaction")
    shipment_tool = _tool(tools, "get_shipment", "get_shipping", "get_delivery")
    customer_tool = _tool(tools, "get_customer_history", "get_customer", "lookup_customer")
    scope = (
        case.get("investigation_scope") if isinstance(case.get("investigation_scope"), dict) else {}
    )
    jobs: list[tuple[str, Any]] = []
    if order_id:
        jobs.extend(
            [
                ("item", collect("order-item-agent", item_tool, order_id=order_id)),
                ("payment", collect("payment-agent", payment_tool, order_id=order_id)),
                ("shipment", collect("shipment-agent", shipment_tool, order_id=order_id)),
            ]
        )
    customer_hint = case.get("customer_unique_id_hint")
    if scope.get("include_customer_history") and isinstance(customer_hint, str):
        jobs.append(
            ("customer", collect("customer-agent", customer_tool, customer_unique_id=customer_hint))
        )
    branch_results = await asyncio.gather(*(job for _, job in jobs)) if jobs else []
    results = dict(zip((role for role, _ in jobs), branch_results, strict=True))
    item_result = results.get("item")
    payment_result = results.get("payment")
    shipment_result = results.get("shipment")
    customer_result = results.get("customer")

    # Policy consumes established evidence, and has no authority to create facts.
    policy_tool = _tool(tools, "get_policy", "lookup_policy")
    policy_version = case.get("policy_version")
    policy_result = (
        await collect("policy-agent", policy_tool, policy_version=policy_version)
        if isinstance(policy_version, str)
        else None
    )
    del policy_result  # Its evidence remains in records and can be referenced by the verifier.
    trace.emit(case_id=case_id, event_type="handoff", actor="policy-agent", target="verifier")

    item_data = item_result.get("data") if item_result else {}
    payment_data = payment_result.get("data") if payment_result else {}
    shipment_data = shipment_result.get("data") if shipment_result else {}
    customer_data = customer_result.get("data") if customer_result else {}
    captured = _amounts(
        payment_data, "captured_total_brl", "captured_amount_brl", "paid_amount_brl"
    )
    refunded = _amounts(payment_data, "refunded_total_brl", "refund_amount_brl")

    shipment_verdict = "insufficient_evidence"
    if _has_status(shipment_data, "seller_delay", "seller late"):
        shipment_verdict = "seller_delay"
    elif _has_status(shipment_data, "logistics_delay", "carrier_delay", "delivery_delay"):
        shipment_verdict = "logistics_delay"
    elif _has_status(shipment_data, "lost"):
        shipment_verdict = "lost"
    elif _has_status(shipment_data, "returned"):
        shipment_verdict = "returned"
    elif _has_status(shipment_data, "on_time", "delivered"):
        shipment_verdict = "on_time"

    payment_verdict = "insufficient_evidence"
    if _has_status(payment_data, "duplicate"):
        payment_verdict = "duplicate_capture"
    elif _has_status(payment_data, "mismatch"):
        payment_verdict = "capture_mismatch"
    elif _has_status(payment_data, "refund_pending"):
        payment_verdict = "refund_pending"
    elif _has_status(payment_data, "refund_failed"):
        payment_verdict = "refund_failed"
    elif captured:
        payment_verdict = "reconciled"

    primary = "insufficient_evidence"
    if shipment_verdict == "seller_delay":
        primary = "late_delivery_seller"
    elif shipment_verdict in {"logistics_delay", "lost", "returned"}:
        primary = "late_delivery_logistics"
    elif payment_verdict == "duplicate_capture":
        primary = "duplicate_charge"
    elif payment_verdict == "capture_mismatch":
        primary = "payment_mismatch"
    elif payment_verdict == "refund_pending":
        primary = "refund_pending"
    elif payment_verdict == "refund_failed":
        primary = "refund_failed"

    evidence_refs = list(dict.fromkeys(result["evidence_ref"] for result in records))
    confidence = (
        0.8 if primary != "insufficient_evidence" and order_id else 0.35 if evidence_refs else 0.0
    )
    related_orders = _values(customer_data, "order_id")
    customer_ids = _values(customer_data, "customer_unique_id", "customer_id")
    recommended_refund = 0.0
    if primary in {"duplicate_charge", "payment_mismatch", "refund_failed"} and captured:
        recommended_refund = max(0.0, max(captured) - sum(refunded))

    claims = request.get("claims") if isinstance(request.get("claims"), list) else []
    claim_assessments = [
        {
            "claim_id": str(claim.get("claim_id") or "unknown")[:64],
            "verdict": "supported" if claim.get("topic") == primary else "insufficient_evidence",
            "confidence": confidence if claim.get("topic") == primary else min(confidence, 0.35),
            "evidence_refs": evidence_refs,
        }
        for claim in claims[:5]
        if isinstance(claim, dict)
    ]
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": [],
            "case_status": "action_required"
            if primary != "insufficient_evidence"
            else "needs_investigation",
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolved,
            "item_ids": _values(item_data, "item_id", "order_item_id"),
            "seller_ids": _values(item_data, "seller_id"),
            "payment_references": _values(
                payment_data, "payment_id", "payment_reference", "transaction_id"
            ),
            "shipment_ids": _values(shipment_data, "shipment_id", "tracking_id"),
        },
        "entity_resolution": {
            "status": resolution_status,
            "resolved_order_ids": resolved,
            "rejected_candidates": [item for item in candidates if item not in resolved],
            "confidence": 0.9 if resolution_status == "resolved" else 0.45 if resolved else 0.0,
        },
        "customer_context": {
            "customer_unique_id": customer_ids[0] if customer_ids else None,
            "related_order_ids": related_orders,
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": _values(item_data, "seller_id")
            if shipment_verdict == "seller_delay"
            else [],
            "timeline_complete": bool(shipment_data),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": sum(captured) if captured else None,
            "refunded_total_brl": sum(refunded) if refunded else None,
            "refundable_total_brl": recommended_refund if captured else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}] if evidence_refs else [],
            "responsible_parties": [],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": (
                [
                    {
                        "reason_code": primary.upper(),
                        "amount_brl": recommended_refund,
                        "entity_id": order_id,
                    }
                ]
                if recommended_refund and order_id
                else []
            ),
        },
        "resolution_actions": ["request_additional_evidence"]
        if primary == "insufficient_evidence"
        else ["review_and_apply_resolution"],
    }
    if claim_assessments:
        output["claim_assessments"] = claim_assessments
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="contract_safe",
        evidence_refs=evidence_refs,
        attributes={
            "evidence_count": len(evidence_refs),
            "entity_resolved": resolution_status == "resolved",
        },
    )
    return output
