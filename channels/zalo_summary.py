"""Group summary generation shared by controller commands and the daily job."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ai import orchestrator
from channels import zalo_repository

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def resolve_window(spec: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(VN_TZ)
    value = spec.strip().lower()
    if value == "7d":
        return current - timedelta(days=7), current
    if value == "homnay":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, current
    if value == "homqua":
        end = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return end - timedelta(days=1), end
    return current - timedelta(hours=24), current


async def _ask(prompt: str) -> str:
    response = await orchestrator.ask(prompt)
    return (getattr(response, "text", None) or "").strip()


async def summarize_group(
    account_id: str,
    target: str,
    start: datetime,
    end: datetime,
) -> tuple[str, str, str]:
    group = await zalo_repository.resolve_group(account_id, target)
    if group is None:
        raise ValueError(f"Không tìm thấy nhóm “{target}”.")
    group_id, alias = group
    rows = await zalo_repository.get_group_messages(account_id, group_id, start, end, limit=2000)
    if not rows:
        return group_id, alias, f"📭 Nhóm {alias} không có tin nhắn trong khoảng đã chọn."

    lines = [
        f"[{sent_at.astimezone(VN_TZ).strftime('%d/%m %H:%M')}] {sender_name or sender_id}: {content}"
        for sender_id, sender_name, content, sent_at in rows
    ]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        if current and current_size + len(line) > 18_000:
            chunks.append("\n".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += len(line) + 1
    if current:
        chunks.append("\n".join(current))

    partials: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        partials.append(await _ask(
            "Bạn đang tóm tắt tin nhắn nhóm Zalo. Chỉ dùng dữ liệu được cung cấp; "
            "không bịa người phụ trách, quyết định hoặc deadline. Phân biệt rõ quyết định, "
            "đề xuất và câu hỏi chưa chốt. Tóm tắt phần " + str(index) + "/" + str(len(chunks)) + ".\n\n" + chunk
        ))

    combined = "\n\n--- PHẦN ---\n\n".join(partials)
    final = await _ask(
        f"Hợp nhất các bản tóm tắt của nhóm {alias} thành báo cáo tiếng Việt ngắn gọn. "
        "Cấu trúc: Tóm tắt nhanh; Chủ đề chính; Quyết định đã chốt; Việc cần làm "
        "(người phụ trách và deadline chỉ khi nguồn nói rõ); Câu hỏi chưa giải quyết; Rủi ro. "
        "Không bịa và không lặp nội dung.\n\n" + combined
    )
    header = (
        f"📋 TỔNG KẾT NHÓM {alias.upper()}\n"
        f"⏱ {start.astimezone(VN_TZ).strftime('%H:%M %d/%m')} → "
        f"{end.astimezone(VN_TZ).strftime('%H:%M %d/%m')}\n"
        f"💬 {len(rows)} tin nhắn\n\n"
    )
    return group_id, alias, header + (final or "Không tạo được nội dung tổng kết.")
