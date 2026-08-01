import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import group_commands


@pytest.mark.asyncio
async def test_add_group_command(monkeypatch):
    saved = {}

    async def fake_add(account_id, group_id, alias):
        saved.update(account_id=account_id, group_id=group_id, alias=alias)

    monkeypatch.setattr(group_commands.zalo_repository, "add_group", fake_add)
    result = await group_commands.maybe_handle_group_command("B", "/themnhom 123 Sale Team")
    assert saved == {"account_id": "B", "group_id": "123", "alias": "sale team"}
    assert "Đã thêm" in result.messages[0]


@pytest.mark.asyncio
async def test_remove_group_command(monkeypatch):
    async def fake_remove(account_id, target):
        return account_id == "B" and target == "sale"

    monkeypatch.setattr(group_commands.zalo_repository, "remove_group", fake_remove)
    result = await group_commands.maybe_handle_group_command("B", "/xoanhom sale")
    assert "Đã ngừng theo dõi" in result.messages[0]


@pytest.mark.asyncio
async def test_unknown_group_command_returns_none():
    assert await group_commands.maybe_handle_group_command("B", "xin chào") is None
