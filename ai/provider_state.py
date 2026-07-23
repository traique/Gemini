"""State machine của provider-chain (cookie|api1|api2). KHÔNG chứa logic gọi
HTTP - chỉ đọc/ghi trạng thái, bền qua restart qua core.database.get_setting/
set_setting, cache trong RAM cho request nhanh.

active_provider chỉ mang tính tham khảo/hiển thị (/status) - quyết định thật
sự ở mỗi request dựa trên cookie_dead_since/api_exhausted_until (xem
ai/orchestrator.py: _run_provider_chain).
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import messages
from core import config, crypto
from core import database as db

logger = logging.getLogger(__name__)

_STATE_ACTIVE_PROVIDER = "provider_active"
_STATE_COOKIE_DEAD_SINCE = "provider_cookie_dead_since"
_STATE_API_EXHAUSTED_PREFIX = "provider_api_exhausted_until_"

# Setting key dùng chung với ai.cookie_client (seed env var __Secure-1PSIDTS
# lúc cache DB được tạo) - đặt ở đây vì ProviderChainState.load() cần nó để
# phát hiện cookie mới được dán sau khi cookie cũ đã bị đánh dấu chết (xem
# docstring load() bên dưới). ai.cookie_client import lại hằng số này thay vì
# định nghĩa riêng, tránh 2 nguồn sự thật cho cùng 1 setting key.
SETTING_PSIDTS_SEED = "gemini_1psidts_seed"


# ─── Cảnh báo Telegram khi cookie chết/sống lại/sắp hết hạn ─────────────────
# Module này không có sẵn bot/chat_id (tránh vòng import với handlers/bot_app)
# - tầng khởi tạo bot đăng ký 1 callback async nhận text, gọi
# set_alert_callback(fn) đúng 1 lần lúc bot_app.build_application().
_alert_callback: Optional[Callable[[str], Awaitable[None]]] = None


def set_alert_callback(fn: Callable[[str], Awaitable[None]]) -> None:
    global _alert_callback
    _alert_callback = fn


def send_alert(text: str) -> None:
    if _alert_callback is None:
        return
    try:
        asyncio.create_task(_alert_callback(text))
    except Exception:
        logger.warning("Không gửi được cảnh báo qua Telegram.", exc_info=True)


class ProviderChainState:
    def __init__(self) -> None:
        self.active_provider: str = "cookie"
        self.cookie_dead_since: Optional[float] = None  # epoch seconds, None = sống/chưa biết
        self.api_exhausted_until: dict[int, float] = {1: 0.0, 2: 0.0}  # epoch seconds
        self._lock = asyncio.Lock()
        self._loaded = False

    async def ensure_loaded(self) -> None:
        if not self._loaded:
            await self.load()

    async def load(self) -> None:
        """Nạp state từ DB lúc khởi động. Nếu chưa gọi, các hàm dùng state
        sẽ tự nạp lười ở lần đầu cần tới (qua ensure_loaded())."""
        async with self._lock:
            if self._loaded:
                return
            raw_active = await db.get_setting(_STATE_ACTIVE_PROVIDER)
            self.active_provider = raw_active if raw_active in ("cookie", "api1", "api2") else "cookie"

            raw_dead = await db.get_setting(_STATE_COOKIE_DEAD_SINCE)
            try:
                self.cookie_dead_since = float(raw_dead) if raw_dead else None
            except ValueError:
                self.cookie_dead_since = None

            if self.cookie_dead_since is not None:
                # Cookie chết ở lần chạy trước. Nếu env var GEMINI_SECURE_1PSIDTS
                # đã đổi so với lần cache gần nhất (người dùng vừa dán cookie
                # mới rồi restart), phải bỏ trạng thái "chết" ngay lúc khởi
                # động - nếu không, _run_provider_chain sẽ bỏ qua cookie ở
                # MỌI request (chỉ probe nền/ /usecookie mới thử lại), khiến
                # logic nhận diện cookie mới trong cookie_client.get_client()
                # không bao giờ có cơ hội chạy vì get_client() không được gọi tới.
                stored_seed = crypto.decrypt(await db.get_setting(SETTING_PSIDTS_SEED))
                if stored_seed != config.GEMINI_SECURE_1PSIDTS:
                    logger.info(
                        "Phát hiện env var GEMINI_SECURE_1PSIDTS đã đổi kể từ lúc "
                        "cookie chết -> reset cookie_dead_since, thử cookie mới ngay."
                    )
                    self.cookie_dead_since = None
                    await db.set_setting(_STATE_COOKIE_DEAD_SINCE, "")

            for idx in (1, 2):
                raw = await db.get_setting(f"{_STATE_API_EXHAUSTED_PREFIX}{idx}")
                try:
                    self.api_exhausted_until[idx] = float(raw) if raw else 0.0
                except ValueError:
                    self.api_exhausted_until[idx] = 0.0

            self._loaded = True
            logger.info(
                "Provider-chain state đã nạp: active=%s, cookie_dead_since=%s, api_exhausted=%s",
                self.active_provider, self.cookie_dead_since, self.api_exhausted_until,
            )

    async def set_active_provider(self, name: str) -> None:
        if self.active_provider != name:
            logger.info("Provider-chain: chuyển active_provider -> %s", name)
        self.active_provider = name
        await db.set_setting(_STATE_ACTIVE_PROVIDER, name)

    async def mark_cookie_dead(self) -> None:
        just_died = self.cookie_dead_since is None
        now = time.time()
        self.cookie_dead_since = now
        await db.set_setting(_STATE_COOKIE_DEAD_SINCE, str(now))
        if just_died:
            send_alert(messages.COOKIE_DEAD_ALERT)

    async def mark_cookie_alive(self) -> None:
        was_dead = self.cookie_dead_since is not None
        self.cookie_dead_since = None
        await db.set_setting(_STATE_COOKIE_DEAD_SINCE, "")
        if was_dead:
            send_alert(messages.COOKIE_ALIVE_ALERT)

    async def mark_api_exhausted(self, idx: int) -> None:
        until = time.time() + config.API_QUOTA_COOLDOWN_SEC
        self.api_exhausted_until[idx] = until
        await db.set_setting(f"{_STATE_API_EXHAUSTED_PREFIX}{idx}", str(until))
        logger.warning("api%s hết quota (429), cooldown %ss.", idx, config.API_QUOTA_COOLDOWN_SEC)

    def api_in_cooldown(self, idx: int) -> bool:
        return time.time() < self.api_exhausted_until.get(idx, 0.0)

    def snapshot(self) -> dict:
        """Snapshot state hiện tại (RAM) - dùng cho /status. Không await DB
        để /status trả lời tức thì phần này."""
        return {
            "active_provider": self.active_provider,
            "cookie_dead_since": self.cookie_dead_since,
            "api1_exhausted_until": self.api_exhausted_until.get(1, 0.0),
            "api2_exhausted_until": self.api_exhausted_until.get(2, 0.0),
        }


provider_state = ProviderChainState()


async def init_provider_state() -> None:
    """Nạp state provider-chain từ DB lúc khởi động (bot_app._post_init gọi 1 lần)."""
    await provider_state.load()


def get_provider_state_snapshot() -> dict:
    return provider_state.snapshot()
