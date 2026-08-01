import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import channel_command_service as service


@pytest.mark.asyncio
async def test_help_command():
    messages, provider = await service.maybe_handle_command(1, "/help")
    assert "/gia" in messages[0]
    assert "/tongket" in messages[0]
    assert provider is None


@pytest.mark.asyncio
async def test_non_command_is_not_handled():
    assert await service.maybe_handle_command(1, "xin chào") is None


@pytest.mark.asyncio
async def test_reset_clears_both_sessions(monkeypatch):
    called = []
    async def reset(): called.append("cookie")
    async def clear(user_id): called.append(("db", user_id))
    monkeypatch.setattr(service.orchestrator, "reset_chat", reset)
    monkeypatch.setattr(service.db, "clear_chat", clear)
    result, _ = await service.maybe_handle_command(7, "/reset")
    assert called == ["cookie", ("db", 7)]
    assert "Đã xoá" in result[0]
