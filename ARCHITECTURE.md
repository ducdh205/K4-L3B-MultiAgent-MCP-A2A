# L3B Architecture Record

## System overview

This repository implements a pure-Python async state machine. The coordinator
routes a case and resolves its candidate order IDs. Once one order is resolved,
the Order/Item, Payment, and Shipment agents collect their evidence in parallel.
The Policy agent consumes only those records, and the Verifier creates the
contract-safe output.

```text
Input → Coordinator / Router → Order/Item Agent ┐
              │ handoff          Payment Agent ─┼→ Policy Agent → Verifier → output
              └→ Entity resolution Shipment Agent┘                     │
                        │                     MCP evidence             └→ trace.jsonl
                        └─ rejected candidates
```

The JSON Schema files in `contracts/schemas/` are the public source of truth.
The workflow never adds implementation fields to outputs, traces, manifests, or
MCP envelopes.

## Agent ownership and permissions

| Actor | Input | Responsibility | Permission | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | case and candidate IDs | bound entity resolution and routing | discovered order lookup | resolution to specialists |
| Order/Item | resolved order ID | items, sellers, products | one discovered item tool | evidence to policy |
| Payment | resolved order ID | captures, duplicate/refund state | one discovered payment tool | evidence to policy |
| Shipment | resolved order ID | delivery timeline | one discovered shipment tool | evidence to policy |
| Customer | enabled hint | related orders | discovered customer tool | evidence to verifier |
| Policy | version and evidence | policy interpretation | discovered policy tool | evidence to verifier |
| Verifier | collected records | output assembly and invariants | none | output and trace |

Discovery is not blanket permission: each role selects at most one discovered,
role-appropriate tool. A missing tool is not guessed or called.

## Entity resolution and A2A protocol

The coordinator considers only supplied candidates, with a hard maximum of five.
It accepts an order only when its authoritative response contains that ID. For a
single candidate, one successful exact lookup also resolves that candidate. All
other candidates are rejected. If zero or multiple IDs remain, the outcome is
`not_found` or `ambiguous`, and order-scoped specialists are skipped.

Every inter-agent handoff carries the case through `case_id` and emits a
`handoff` trace event. The path is strictly acyclic: coordinator → specialists
→ policy → verifier. There is no automatic retry: MCP retries can double audit
cost and make non-idempotent behavior unsafe.

## Evidence and conflicts

`EvidenceGateway` validates every MCP response against
`mcp-evidence-response-v1.schema.json`. A consumed result emits exactly its
gateway-issued `evidence_ref` in `tool_result_consumed`. References are stored
only in the current invocation, so evidence cannot cross case boundaries.

The verifier derives entities, status and amounts from MCP `data` only. Missing
or conflicting evidence produces `insufficient_evidence`, never a fabricated
fact. `data_conflicts` stays empty unless an authoritative conflict can be
represented by the public output contract. Claim evidence, top-level evidence,
and trace evidence use the same collected references.

## Failure and efficiency policy

| Failure | Retry | Fallback | Observable outcome |
| --- | ---: | --- | --- |
| MCP error or timeout | 0 | omit result; use insufficient evidence | `tool_unavailable` |
| Unresolved entity | 0 | skip order-scoped specialists | `handoff` with resolution status |
| Missing/conflicting source | 0 | no speculative refund or action | verifier `contract_safe` |
| Invalid MCP envelope | 0 | gateway rejects it | unvalidated data never reaches output |

Tool discovery is cached for a gateway lifetime. Equivalent calls are
deduplicated in a case; candidate resolution is capped; only independent
specialist calls run concurrently. No cross-case cache is used.

## Verification invariants

- Required fields and field names match `l3b-output-v2.schema.json` exactly.
- Output `case_id`, evidence references, and trace events remain case-scoped.
- Resolved and rejected candidates are disjoint.
- Entity, shipment, payment and customer values come from evidence.
- Monetary values are non-negative BRL; confidence remains in `[0, 1]`.
- Missing timeline/evidence maps to `needs_investigation`, not an invented conclusion.
- The CLI re-validates output, trace and manifest before persistence/package.

## Reproducibility

Python 3.11+ and dependency ranges are declared in `pyproject.toml`. The
workflow uses no model call, random seed, or secret-bearing logs. Its concurrent
branch has at most four independent calls (three specialists plus customer).

```bash
python -m pip install -e ".[dev]"
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

The Team API key remains in `.env` and is never written to a submission or trace.
