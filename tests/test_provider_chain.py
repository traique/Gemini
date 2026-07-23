"""Unit test cho provider-chain (ai.orchestrator._run_provider_chain).

Chạy: pytest tests/test_provider_chain.py -v

State provider-chain sống trong ai.provider_state.provider_state (1 singleton
ProviderChainState) - các test dưới đây tự reset qua fixture `reset_state`
trước mỗi test để không bị rò rỉ giữa các case.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import orchestrator  # noqa: E402
from ai.provider_state import _STATE_COOKIE_DEAD_SINCE, provider_state  # noqa: E402
from core import config, database as db  # noqa: E402


class FakeSettingsStore:
    """Thay thế db.get_setting/set_setting bằng dict trong RAM, để test
    không cần Postgres thật."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeSettingsStore()
    monkeypatch.setattr(db, "get_setting", store.get)
    monkeypatch.setattr(db, "set_setting", store.set)
    return store


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, fake_store):
    """Reset toàn bộ state của provider_state trước mỗi test, và mock 2 API
    key mặc định đã cấu hình (test tự override nếu cần khác)."""
    provider_state.active_provider = "cookie"
    provider_state.cookie_dead_since = None
    provider_state.api_exhausted_until = {1: 0.0, 2: 0.0}
    provider_state._loaded = False

    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "fake-key-1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", "fake-key-2")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["cookie", "api1", "api2"])

    # reset_client() thật sẽ gọi client.close() (I/O) - không cần thiết cho
    # test logic thuần provider-chain, mock thành no-op.
    async def _fake_reset_client():
        return None

    from ai import cookie_client

    monkeypatch.setattr(cookie_client, "reset_client", _fake_reset_client)
    yield


def _ok_cookie_call():
    async def _call():
        return "cookie-response"

    return _call


def _failing_cookie_call():
    async def _call():
        raise RuntimeError("cookie treo/lỗi mô phỏng")

    return _call


class QuotaExhaustedError(Exception):
    """Mô phỏng lỗi 429/RESOURCE_EXHAUSTED thật của google-genai."""

    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED: quota exceeded")
        self.code = 429


@pytest.mark.asyncio
async def test_cookie_song_thi_dung_cookie(fake_store):
    """Cookie sống bình thường -> luôn dùng cookie, không chạm tới api_call."""

    async def api_call(idx):
        raise AssertionError("Không được gọi api_call khi cookie đang sống")

    result = await orchestrator._run_provider_chain(
        cookie_call=_ok_cookie_call(), api_call=api_call
    )
    assert result == "cookie-response"
    assert provider_state.active_provider == "cookie"


@pytest.mark.asyncio
async def test_cookie_chet_chuyen_sang_api1(fake_store):
    """Cookie lỗi -> đánh dấu chết + active_provider chuyển sang api1."""
    api_calls = []

    async def api_call(idx):
        api_calls.append(idx)
        if idx == 1:
            return "api1-response"
        raise AssertionError("Không nên rơi xuống api2 khi api1 đã thành công")

    result = await orchestrator._run_provider_chain(
        cookie_call=_failing_cookie_call(), api_call=api_call
    )

    assert result == "api1-response"
    assert api_calls == [1]
    assert provider_state.active_provider == "api1"
    assert provider_state.cookie_dead_since is not None
    # State phải được persist vào DB (qua db.set_setting đã mock), không chỉ
    # ở RAM - để sống qua restart như thiết kế gốc.
    assert fake_store.data.get(_STATE_COOKIE_DEAD_SINCE)


@pytest.mark.asyncio
async def test_cookie_chet_roi_khong_thu_lai_cookie_o_request_sau(fake_store):
    """Sau khi cookie đã bị đánh dấu chết ở 1 request trước, request MỚI
    không được thử cookie nữa (chỉ probe nền/,/usecookie mới thử lại)."""
    cookie_call_count = 0

    async def cookie_call():
        nonlocal cookie_call_count
        cookie_call_count += 1
        return "cookie-response"  # nếu bị gọi, coi như lỗi thiết kế

    async def api_call(idx):
        return f"api{idx}-response"

    provider_state.cookie_dead_since = time.time() - 100  # đã chết từ trước
    provider_state._loaded = True  # tránh ensure_loaded() nạp đè từ DB rỗng

    result = await orchestrator._run_provider_chain(cookie_call=cookie_call, api_call=api_call)

    assert result == "api1-response"
    assert cookie_call_count == 0, "Cookie đã biết chết -> KHÔNG được thử lại ở request thường"


@pytest.mark.asyncio
async def test_api1_het_quota_chuyen_api2_va_cooldown(fake_store):
    """api1 lỗi 429 -> đánh dấu cooldown + chuyển sang api2, không phải lỗi
    thường (điểm quan trọng: official_client.is_quota_exhausted_error phải
    phân biệt được)."""
    provider_state.cookie_dead_since = time.time() - 100  # bỏ qua nhánh cookie cho gọn
    provider_state._loaded = True

    async def api_call(idx):
        if idx == 1:
            raise QuotaExhaustedError()
        return "api2-response"

    result = await orchestrator._run_provider_chain(
        cookie_call=_failing_cookie_call(), api_call=api_call
    )

    assert result == "api2-response"
    assert provider_state.active_provider == "api2"
    assert provider_state.api_in_cooldown(1), "api1 phải được đánh dấu cooldown sau lỗi 429"
    assert not provider_state.api_in_cooldown(2)


@pytest.mark.asyncio
async def test_api1_dang_cooldown_bi_bo_qua_ngay_khong_goi_lai(fake_store):
    """api1 đang trong thời gian cooldown -> KHÔNG được gọi lại, nhảy thẳng
    sang api2 (khác với lỗi 429 mới - ở đây api_call(1) không được gọi)."""
    provider_state.cookie_dead_since = time.time() - 100
    provider_state.api_exhausted_until[1] = time.time() + 999  # đang cooldown
    provider_state._loaded = True  # tránh ensure_loaded() nạp đè từ DB rỗng

    called_idx = []

    async def api_call(idx):
        called_idx.append(idx)
        return f"api{idx}-response"

    result = await orchestrator._run_provider_chain(
        cookie_call=_failing_cookie_call(), api_call=api_call
    )

    assert result == "api2-response"
    assert called_idx == [2], "api1 đang cooldown -> không được gọi lại"


@pytest.mark.asyncio
async def test_dao_provider_order_api_truoc_cookie(fake_store, monkeypatch):
    """PROVIDER_ORDER=api1,api2,cookie -> phải thử api1 TRƯỚC cookie, kể cả
    khi cookie đang sống bình thường (đảo ưu tiên)."""
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1", "api2", "cookie"])

    call_order = []

    async def cookie_call():
        call_order.append("cookie")
        return "cookie-response"

    async def api_call(idx):
        call_order.append(f"api{idx}")
        return f"api{idx}-response"

    result = await orchestrator._run_provider_chain(cookie_call=cookie_call, api_call=api_call)

    assert result == "api1-response"
    assert call_order == ["api1"], "Cookie không được thử khi api1 đứng đầu order và đã thành công"


@pytest.mark.asyncio
async def test_moi_provider_deu_that_bai_thi_raise_loi_cuoi(fake_store):
    """Cookie chết + cả 2 API đều cooldown -> cứu cánh cuối thử lại các
    provider known-bad; nếu vẫn thất bại hết, phải raise lỗi (không được
    nuốt lỗi và trả None/im lặng)."""
    provider_state.cookie_dead_since = time.time() - 100
    provider_state.api_exhausted_until = {1: time.time() + 999, 2: time.time() + 999}
    provider_state._loaded = True

    async def api_call(idx):
        raise RuntimeError(f"api{idx} vẫn lỗi ở lượt cứu cánh")

    with pytest.raises(Exception):
        await orchestrator._run_provider_chain(
            cookie_call=_failing_cookie_call(), api_call=api_call
        )


@pytest.mark.asyncio
async def test_chua_cau_hinh_api_thi_retry_cookie_1_lan(fake_store, monkeypatch):
    """Không có API key nào (hành vi gốc trước khi có provider-chain) ->
    cookie lỗi thì reset + thử lại cookie đúng 1 lần, không raise ngay."""
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", None)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)

    attempts = []

    async def cookie_call():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("lỗi lần đầu")
        return "cookie-response-lan-2"

    async def api_call(idx):
        raise AssertionError("Không có key nào cấu hình -> không được gọi api_call")

    result = await orchestrator._run_provider_chain(cookie_call=cookie_call, api_call=api_call)

    assert result == "cookie-response-lan-2"
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_co_api_van_retry_cookie_1_lan_truoc_khi_khai_tu(fake_store):
    """Có cấu hình API key nhưng cookie chỉ lỗi THOÁNG QUA (thành công ngay ở
    lần retry) -> không được khai tử oan (cookie_dead_since phải None) và
    không được rơi xuống api_call, vì cookie đã hồi trong chính request này."""
    attempts = []

    async def cookie_call():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("lỗi thoáng qua lần đầu")
        return "cookie-response-lan-2"

    async def api_call(idx):
        raise AssertionError("Cookie đã hồi ở lần retry -> không được rơi xuống api_call")

    result = await orchestrator._run_provider_chain(cookie_call=cookie_call, api_call=api_call)

    assert result == "cookie-response-lan-2"
    assert len(attempts) == 2
    assert provider_state.cookie_dead_since is None
