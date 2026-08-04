import pytest

from channels import zalo_repository


@pytest.mark.asyncio
async def test_cleanup_old_messages_binds_retention_as_integer(monkeypatch):
    calls = []

    class FakePool:
        async def execute(self, query, *args):
            calls.append((query, args))

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setenv("ZALO_GROUP_RETENTION_DAYS", "30")
    monkeypatch.setattr(zalo_repository.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(zalo_repository, "ensure_schema", fake_ensure_schema)

    await zalo_repository.cleanup_old_messages("zalo-bot")

    query, args = calls[0]
    assert "$2::integer" in query
    assert args == ("zalo-bot", 30)
