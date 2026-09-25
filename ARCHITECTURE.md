# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## System overview

Repository triển khai một state machine bất đồng bộ thuần Python. Coordinator
định tuyến case và phân giải các candidate order ID. Khi có đúng một order được
resolve, các agent Order/Item, Payment và Shipment thu thập evidence song song.
Policy Agent chỉ dùng các record đã thu thập; Verifier tạo output an toàn theo
contract.

```text
Input → Coordinator / Router → Order/Item Agent ┐
              │ handoff          Payment Agent ─┼→ Policy Agent → Verifier → output
              └→ Entity resolution Shipment Agent┘                     │
                        │                     MCP evidence             └→ trace.jsonl
                        └─ rejected candidates
```

Các JSON Schema trong `contracts/schemas/` là nguồn chân lý công khai.
Workflow không thêm field cài đặt nội bộ vào output, trace, manifest hoặc MCP
envelope.

## Agent ownership

| Actor | Input | Trách nhiệm | Quyền hạn | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | case và candidate ID | entity resolution có giới hạn, định tuyến | order lookup đã discovery | kết quả resolution cho specialists |
| Order/Item | order ID đã resolve | item, seller, product | một item tool đã discovery | evidence cho policy |
| Payment | order ID đã resolve | capture, trạng thái duplicate/refund | một payment tool đã discovery | evidence cho policy |
| Shipment | order ID đã resolve | timeline giao hàng | một shipment tool đã discovery | evidence cho policy |
| Customer | hint được bật | các order liên quan | customer tool đã discovery | evidence cho verifier |
| Policy | version và evidence | diễn giải policy | policy tool đã discovery | evidence cho verifier |
| Verifier | record đã thu thập | dựng output và kiểm tra invariant | không có | output và trace |

Discovery không phải là quyền gọi tất cả tool: mỗi role chỉ chọn tối đa một tool
phù hợp đã được discovery. Tool không tồn tại sẽ không bị đoán tên hoặc gọi.

## Entity resolution và A2A protocol

Coordinator chỉ xét candidate được cung cấp, với giới hạn cứng là năm ID. Một
order chỉ được chấp nhận khi response authoritative chứa chính ID đó. Với một
candidate duy nhất, một exact lookup thành công cũng resolve candidate đó. Các
candidate còn lại bị reject. Nếu có không hoặc nhiều ID còn lại, trạng thái là
`not_found` hoặc `ambiguous` và specialist cần order sẽ không chạy.

Mọi handoff liên kết theo `case_id` và emit sự kiện trace `handoff`. Luồng luôn
không có chu trình: coordinator → specialists → policy → verifier. Không retry
tự động; retry MCP có thể tăng audit cost và không an toàn với thao tác không
idempotent.

## Evidence và conflict lifecycle

`EvidenceGateway` validate mọi MCP response bằng
`mcp-evidence-response-v1.schema.json`. Một kết quả được dùng emit đúng
`evidence_ref` do gateway cấp trong `tool_result_consumed`. Reference chỉ được
lưu trong invocation hiện tại nên không thể dùng chéo case.

Verifier chỉ suy ra entity, trạng thái và số tiền từ MCP `data`. Evidence thiếu
hoặc mâu thuẫn tạo `insufficient_evidence`, không tạo dữ kiện giả. Trường
`data_conflicts` để rỗng cho tới khi conflict authoritative có thể biểu diễn
bằng public output contract. Evidence của claim, evidence top-level và trace
dùng chung các reference đã thu thập.

## Failure and efficiency policy

| Failure | Retry | Fallback | Kết quả quan sát được |
| --- | ---: | --- | --- |
| MCP lỗi hoặc timeout | 0 | bỏ result, dùng insufficient evidence | `tool_unavailable` |
| Entity chưa resolve | 0 | bỏ specialists cần order | `handoff` có trạng thái resolution |
| Source thiếu/mâu thuẫn | 0 | không refund hoặc action suy đoán | verifier `contract_safe` |
| MCP envelope không hợp lệ | 0 | gateway từ chối | data chưa validate không đến output |

Tool discovery được cache theo vòng đời gateway. Call tương đương được khử lặp
trong một case; candidate resolution bị giới hạn; chỉ các specialist độc lập
chạy đồng thời. Không có cache chéo case.

## Verification invariants

- Required field và tên field khớp chính xác `l3b-output-v2.schema.json`.
- `case_id`, evidence reference và trace event của output luôn thuộc case hiện tại.
- Resolved candidate và rejected candidate không giao nhau.
- Giá trị entity, shipment, payment và customer đều có nguồn evidence.
- Monetary value là BRL không âm; confidence luôn trong `[0, 1]`.
- Timeline/evidence thiếu được map về `needs_investigation`, không phải kết luận giả.
- CLI validate lại output, trace và manifest trước khi ghi hoặc đóng gói.

## Reproducibility

Python 3.11+ và dependency range được khai báo trong `pyproject.toml`. Workflow
không dùng model call, random seed hay log có secret. Nhánh concurrent có tối đa
bốn call độc lập (ba specialist và customer).

```bash
python -m pip install -e ".[dev]"
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Team API key nằm trong `.env`, không được ghi vào submission hoặc trace.
