"""Facade công khai của package `ai`: kết hợp nhánh cookie (ai/cookie_client.py)
và nhánh api1/api2 (ai/official_client.py) thành provider-chain có fallback,
theo thứ tự core.config.PROVIDER_ORDER (mặc định cookie -> api1 -> api2).

- Cookie chết -> chuyển hẳn sang API, KHÔNG thử lại cookie mỗi tin nhắn nữa.
  Chỉ 3 cách quay lại cookie: probe nền định kỳ, seed env cookie mới, lệnh
  /usecookie (xem init_provider_state()/start_background_tasks()/try_cookie_now()).
- api1 hết quota (429/ResourceExhausted) -> cooldown API_QUOTA_COOLDOWN_SEC rồi
  tự thử lại; trong lúc đó dùng api2 nếu có.
"""
import asyncio
import logging
from typing import Optional

from core import config, database as db
from ai import cookie_client, official_client
from ai import provider_state as provider_state_module
from ai.provider_state import provider_state

logger = logging.getLogger(__name__)

# Chỉ 1 tài khoản / 1 session -> serialize mọi request qua provider-chain.
call_lock = asyncio.Lock()

# Trần thời gian tối đa cho MỘT lượt gọi qua cookie (init + generate/send),
# tính cả các bước generate_content()/send_message() không có tham số
# timeout riêng - nếu không chặn ở đây, 1 lượt treo mạng có thể kéo dài tới
# vài phút trước khi gemini-webapi tự bỏ cuộc, làm fallback bị trễ theo.
_CALL_TIMEOUT_SEC = config.GEMINI_COOKIE_CALL_TIMEOUT_SEC


async def _run_with_call_timeout(call_fn):
    return await asyncio.wait_for(call_fn(), timeout=_CALL_TIMEOUT_SEC)


async def init_provider_state() -> None:
    """Nạp state provider-chain từ DB lúc khởi động (bot_app._post_init gọi 1 lần)."""
    await provider_state_module.init_provider_state()


def set_alert_callback(fn) -> None:
    provider_state_module.set_alert_callback(fn)


def get_provider_state_snapshot() -> dict:
    return provider_state.snapshot()


# ─── Provider-chain dispatcher (tổng quát hoá theo config.PROVIDER_ORDER) ──
async def _run_provider_chain(*, cookie_call, api_call):
    """cookie_call: async callable () -> ModelOutput (gemini-webapi).
    api_call: async callable (idx: int) -> official_client.FallbackResponse.

    Thứ tự thử: theo config.PROVIDER_ORDER (mặc định cookie -> api1 -> api2,
    đổi được qua env PROVIDER_ORDER - xem README mục Provider-chain).

    - "cookie" trong order: bỏ qua nếu đã biết chết (cookie_dead_since is not
      None) - không retry mỗi tin nhắn, chỉ probe nền/lệnh /usecookie mới
      thử lại.
    - "apiN" trong order: bỏ qua nếu đang cooldown quota; 429 -> đánh dấu
      cooldown rồi thử provider kế trong order.
    - Nếu chưa cấu hình key của 1 apiN nào đó, provider đó bị loại hẳn khỏi
      order (không tính là "known-bad" để cứu cánh cuối, vì cấu hình thiếu
      không phải sự cố tạm thời).
    - Cứu cánh cuối: nếu KHÔNG provider nào trong order thành công, thử lại
      đúng 1 lần các provider đã bị bỏ qua vì "known-bad" (cookie chết / api
      cooldown) - để bot không hoàn toàn im lặng nếu vô tình mọi provider ưu
      tiên đều đang gặp sự cố tạm thời cùng lúc.
    """
    await provider_state.ensure_loaded()

    async def _attempt_cookie():
        result = await _run_with_call_timeout(cookie_call)
        await provider_state.mark_cookie_alive()
        await provider_state.set_active_provider("cookie")
        return result

    async def _attempt_api(idx: int):
        result = await api_call(idx)
        await provider_state.set_active_provider(f"api{idx}")
        return result

    async with call_lock:
        last_exc: Optional[BaseException] = None
        known_bad_skipped: list[str] = []

        for provider in config.PROVIDER_ORDER:
            if provider == "cookie":
                if provider_state.cookie_dead_since is not None:
                    known_bad_skipped.append("cookie")
                    continue
                has_api = bool(official_client.api_key_for(1) or official_client.api_key_for(2))
                try:
                    return await _attempt_cookie()
                except Exception as e:
                    last_exc = e
                    logger.warning(
                        "Gọi Gemini (cookie) lỗi/treo lần 1, reset và thử lại 1 lần trước khi "
                        "coi là cookie chết (tránh khai tử oan vì 1 lỗi thoáng qua).",
                        exc_info=True,
                    )
                    await cookie_client.reset_client()
                    try:
                        return await _attempt_cookie()
                    except Exception as e2:
                        last_exc = e2
                        if not has_api:
                            # Không có fallback nào cấu hình -> đã thử lại hết
                            # cách, để lỗi lộ ra ngay thay vì rơi vào im lặng.
                            raise
                        logger.warning(
                            "Cookie Gemini vẫn lỗi sau khi thử lại (quá %ss hoặc lỗi khác), "
                            "đánh dấu chết và chuyển sang provider kế trong order.",
                            _CALL_TIMEOUT_SEC, exc_info=True,
                        )
                        await provider_state.mark_cookie_dead()
            else:
                idx = 1 if provider == "api1" else 2
                if not official_client.api_key_for(idx):
                    continue  # Chưa cấu hình key -> loại hẳn, không phải "known-bad" tạm thời.
                if provider_state.api_in_cooldown(idx):
                    known_bad_skipped.append(provider)
                    continue
                try:
                    return await _attempt_api(idx)
                except Exception as e:
                    if official_client.is_quota_exhausted_error(e):
                        await provider_state.mark_api_exhausted(idx)
                        last_exc = e
                        continue
                    logger.exception("%s lỗi (không phải hết quota).", provider)
                    last_exc = e
                    continue

        # Cứu cánh cuối: thử lại các provider "known-bad" đã bị bỏ qua, đúng 1 lần.
        for provider in known_bad_skipped:
            try:
                if provider == "cookie":
                    return await _attempt_cookie()
                idx = 1 if provider == "api1" else 2
                return await _attempt_api(idx)
            except Exception as e:
                last_exc = e
                continue

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            "Không có provider nào khả dụng (cookie lỗi, chưa cấu hình "
            "GOOGLE_AI_STUDIO_API_KEY_1/2, hoặc cả 2 đều đang cooldown quota)."
        )


async def ask(prompt: str, model: Optional[str] = None, enable_search: bool = False):
    """Tác vụ 1 lượt (vd narrative phân tích cổ phiếu) - không cần trí nhớ,
    không cần persona system_instruction (prompt đã tự chứa persona rút gọn).

    model: tên model tường minh do caller truyền vào (ưu tiên cao nhất).
    Không truyền -> lấy model ưu tiên đã lưu qua lệnh /model."""
    model_name = model or await cookie_client.get_preferred_model_name()

    async def _cookie_call():
        # Resolve model_obj TẠI ĐÂY (không phải trước khi vào provider-chain)
        # - find_model()/get_client() tự khởi tạo cookie client nếu chưa có,
        # có thể ném exception nếu cookie đã chết. _run_provider_chain đã
        # biết bỏ qua "cookie" khi cookie_dead_since is not None nên hàm này
        # sẽ không được gọi trong trường hợp đó; nếu resolve trước ở ngoài,
        # exception sẽ làm chết cả ask() trước khi kịp thử api1/api2.
        client = await cookie_client.get_client()
        model_obj = await cookie_client.find_model(model_name) if model_name else None
        kwargs = {"model": model_obj} if model_obj else {}
        return await client.generate_content(prompt, **kwargs)

    async def _api_call(idx: int):
        return await official_client.generate(idx, prompt, model=model_name, enable_search=enable_search)

    return await _run_provider_chain(cookie_call=_cookie_call, api_call=_api_call)


async def chat(user_id: int, prompt: str, grounding: str = "", memory_context: str = ""):
    """Chat tự nhiên (persona Lan Anh) qua provider-chain.
    - Cookie: ChatSession (gemini-webapi) - Google giữ lịch sử phía họ. Vì
      lịch sử được Google lưu vĩnh viễn cho session, memory_context (trí nhớ
      dài hạn, không đổi trong phiên) chỉ chèn vào LƯỢT ĐẦU TIÊN của mỗi
      phiên mới; grounding (giá thực tế, đổi liên tục) chèn ở MỌI lượt.
    - API (api1/api2): stateless, nên memory_context/grounding + lịch sử
      cửa sổ trượt (core.database.get_session_messages()) được nạp lại ở MỌI lượt.
    handlers/chat_router.py ghi (user, model) vào chat_messages sau khi có
    phản hồi thành công, để mọi provider dùng chung 1 nguồn trí nhớ.

    grounding: dữ liệu giá thực tế (vd từ DNSE) để Gemini bám số thật thay
    vì tự bịa. memory_context: trí nhớ dài hạn (user_facts + rolling
    summary, xem services.memory_service.build_memory_context()). Cả hai chỉ
    chèn vào request gửi đi, không lưu vào chat_messages."""
    full_prompt = prompt
    if grounding:
        full_prompt = f"{grounding}\n\n{full_prompt}"
    if memory_context:
        full_prompt = f"{memory_context}\n\n{full_prompt}"

    async def _cookie_call():
        is_new_session = await cookie_client.ensure_chat_session()
        prompt_with_time = f"{official_client.now_vn_context()}\n{prompt}"
        # grounding (giá thực tế) phải chèn ở MỌI lượt - giá thay đổi theo
        # thời gian nên chỉ nạp ở lượt đầu (như memory_context) sẽ khiến
        # Gemini tự bịa giá từ lượt 2 trở đi vì không còn số thật để bám.
        # memory_context (trí nhớ dài hạn) thì không đổi trong 1 phiên nên
        # vẫn chỉ cần nạp đúng 1 lần lúc mở phiên mới, tránh lặp/phình token.
        if grounding:
            prompt_with_time = f"{grounding}\n\n{prompt_with_time}"
        if is_new_session and memory_context:
            prompt_with_time = f"{memory_context}\n\n{prompt_with_time}"
        return await cookie_client.get_chat_session().send_message(prompt_with_time)

    async def _api_call(idx: int):
        history = await db.get_session_messages(
            user_id, config.CHAT_HISTORY_TURNS, config.CHAT_SESSION_TIMEOUT_SEC
        )
        preferred_model = await cookie_client.get_preferred_model_name()
        return await official_client.generate(
            idx,
            full_prompt,
            system_instruction=config.load_chat_skill(),
            history=history,
            persona_generation_config=True,
            enable_search=True,
            model=preferred_model,
        )

    return await _run_provider_chain(cookie_call=_cookie_call, api_call=_api_call)


async def reset_chat() -> None:
    """Xoá ChatSession phía cookie. handlers/commands.py reset_chat_cmd() gọi
    thêm core.database.clear_chat(user_id) để xoá luôn cửa sổ trượt phía API."""
    async with call_lock:
        cookie_client.clear_chat_session()


async def analyze_image(instruction: str, image_path: str):
    """Ảnh -> prompt (không cần trí nhớ). Chạy qua provider-chain: cookie
    (upload file qua session web) hoặc api1/api2 (vision qua SDK chính thức)."""

    async def _cookie_call():
        client = await cookie_client.get_client()
        return await client.generate_content(instruction, files=[image_path])

    async def _api_call(idx: int):
        return await official_client.generate_image_prompt(idx, instruction, image_path)

    return await _run_provider_chain(cookie_call=_cookie_call, api_call=_api_call)


async def check_cookie_status() -> tuple[bool, str]:
    """Xác nhận cookie còn dùng được thật với server, không chỉ kiểm tra
    client đã init trong RAM (cookie_client.get_client() có thể trả về client
    cache dù cookie đã hết hạn phía Google)."""
    try:
        client = await cookie_client.get_client()
        await client.generate_content("ping")
        return True, "OK"
    except Exception as e:
        # Không gọi reset_client() ở đây để tránh phá state của các request
        # khác đang chạy song song một cách không cần thiết.
        return False, f"{type(e).__name__}: {e}"


async def check_ai_studio_status(idx: int) -> tuple[bool, str]:
    """Re-export tiện dụng của official_client.check_ai_studio_status(), để
    handlers/commands.py (lệnh /status) chỉ cần import mỗi ai.orchestrator
    cho mọi kiểm tra trạng thái provider, thay vì phải import thêm
    ai.official_client chỉ vì 1 hàm này."""
    return await official_client.check_ai_studio_status(idx)


async def try_cookie_now() -> tuple[bool, str]:
    """Ép thử cookie ngay (lệnh /usecookie). Thành công
    -> chuyển active_provider về cookie và clear cookie_dead_since ngay."""
    await provider_state.ensure_loaded()
    async with call_lock:
        ok, detail = await check_cookie_status()
        if ok:
            await provider_state.mark_cookie_alive()
            await provider_state.set_active_provider("cookie")
        return ok, detail


# ─── Probe nền tự quay về cookie ───────────────────────────────────────────
_probe_task: Optional[asyncio.Task] = None


async def _cookie_probe_loop() -> None:
    while True:
        await asyncio.sleep(config.COOKIE_PROBE_INTERVAL_SEC)
        await provider_state.ensure_loaded()
        if provider_state.cookie_dead_since is None:
            continue  # cookie đang sống (hoặc chưa từng lỗi) -> không cần probe
        try:
            async with call_lock:
                ok, _ = await check_cookie_status()
                if ok:
                    await provider_state.mark_cookie_alive()
                    await provider_state.set_active_provider("cookie")
        except Exception:
            logger.warning("Lỗi khi probe cookie nền.", exc_info=True)


def start_background_tasks() -> None:
    """Gọi 1 lần lúc khởi động bot (bot_app._post_init) để bật probe nền."""
    global _probe_task
    if _probe_task is None or _probe_task.done():
        _probe_task = asyncio.create_task(_cookie_probe_loop())
