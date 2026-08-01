"""Deterministic controller commands for the Zalo group allowlist and summaries."""
import asyncpg

from channels import zalo_repository
from channels.zalo_summary import resolve_window, summarize_group
from services.channel_chat_service import ChannelResult


async def maybe_handle_group_command(account_id: str, text: str) -> ChannelResult | None:
    raw = text.strip()
    command = raw.split(maxsplit=1)[0].lower() if raw else ""

    if command == "/nhom":
        groups = await zalo_repository.list_groups(account_id)
        if not groups:
            return ChannelResult(["Chưa theo dõi nhóm nào. Dùng /nhomzalo để xem ID, sau đó /themnhom <group_id> <tên-gợi-nhớ>."])
        lines = ["📚 Các nhóm đang theo dõi:"]
        lines.extend(f"{index}. {alias} — {group_id}" for index, (group_id, alias) in enumerate(groups, 1))
        return ChannelResult(["\n".join(lines)])

    if command == "/themnhom":
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /themnhom <group_id> <tên-gợi-nhớ>"])
        group_id = parts[1].strip()
        alias = (parts[2].strip() if len(parts) == 3 else group_id).lower()
        if not group_id or not alias or len(alias) > 100:
            return ChannelResult(["Group ID hoặc tên gợi nhớ không hợp lệ."])
        try:
            await zalo_repository.add_group(account_id, group_id, alias)
        except asyncpg.UniqueViolationError:
            return ChannelResult([f"Tên gợi nhớ “{alias}” đang được dùng cho nhóm khác."])
        return ChannelResult([f"✅ Đã thêm nhóm {alias} ({group_id})."])

    if command == "/xoanhom":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /xoanhom <group_id hoặc tên-gợi-nhớ>"])
        target = parts[1].strip()
        removed = await zalo_repository.remove_group(account_id, target)
        if not removed:
            return ChannelResult([f"Không tìm thấy nhóm “{target}”."])
        return ChannelResult([f"✅ Đã ngừng theo dõi và xóa dữ liệu đã lưu của nhóm {target}."])

    if command == "/tongket":
        parts = raw.split()
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /tongket <nhóm> [24h|7d|homnay|homqua]"])
        target = parts[1]
        spec = parts[2] if len(parts) >= 3 else "24h"
        start, end = resolve_window(spec)
        try:
            _, _, content = await summarize_group(account_id, target, start, end)
            return ChannelResult([content])
        except ValueError as exc:
            return ChannelResult([str(exc)])

    return None
