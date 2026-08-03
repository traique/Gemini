"""Facade công khai của package `ai`: kết hợp nhánh cookie (ai/cookie_client.py)
và nhánh api1/api2 (ai/official_client.py) thành provider-chain có fallback,
theo thứ tự core.config.PROVIDER_ORDER (mặc định cookie -> api1 -> api2).

- Cookie chết -> chuyển hẳn sang API, KHÔNG thử lại cookie mọi tin nhắn nữa.
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
call_lock = asyncio.Lock()


def _call_timeout_sec() -> float:
    return config.GEMINI_COOKIE_CALL_TIMEOUT_SEC


async def _run_with_call_timeout(call_fn):
    return await asyncio.wait_for(call_fn(), timeout=_call_timeout_sec())


async def init_provider_state() -> None:
    await provider_state_module.init_provider_state()


def set_alert_callback(fn) -> None:
    provider_state_module.set_alert_callback(fn)


def get_provider_state_snapshot() -> dict:
    return provider_state.snapshot()


class RealSearchUnavailableError(RuntimeError):
    """Strict grounded search has no configured official API provider."""


def _search_only_providers() -> list[str]:
    """Return configured official providers or fail closed.

    Cookie requests cannot guarantee a real Google Search tool, so strict
    search must never silently fall back to that provider.
    """
    available = {
        "api1": bool(official_client.api_key_for(1)),
        "api2": bool(official_client.api_key_for(2)),
    }
    order = [provider for provider in config.PROVIDER_ORDER if available.get(provider, False)]
    if not order:
        raise RealSearchUnavailableError(
            "Tác vụ yêu cầu Google Search thật nhưng chưa cấu hình "
            "GOOGLE_AI_STUDIO_API_KEY_1/2."
        )
    return order


_FORCED_SEARCH_DIRECTIVE = (
    "[YÊU CẦU BẮT BUỘC TỪ HỆ THỐNG]\n"
    "Câu hỏi này cần số liệu/sự kiện thực tế bên ngoài sàn chứng khoán Việt Nam "
    "(giá hàng hoá, tỷ giá, crypto, chỉ số quốc tế, tin thời sự). Hệ thống KHÔNG "
    "có sẵn dữ liệu này để cung cấp cho bạn.\n"
    "1. BẮT BUỘC dùng Google Search để tra trước khi trả lời.\n"
    "2. CHỈ được nêu con số, mốc thời gian và sự kiện có TRONG kết quả tra cứu. "
    "Kèm theo thời điểm của số liệu và tên nguồn.\n"
    "3. Nếu tra không ra dữ liệu: nói thẳng là chưa tra được và DỪNG LẠI. "
    "TUYỆT ĐỐI KHÔNG đưa ra bất kỳ con số hay sự kiện nào từ trí nhớ, không ước "
    "lượng, không suy diễn. Trả lời \"em chưa tra được\" là ĐÚNG; đoán một con "
    "số nghe hợp lý là SAI nghiêm trọng."
)


async def _run_provider_chain(*, cookie_call, api_call, providers_override: Optional[list[str]] = None):
    """Run cookie/api providers in configured order with persisted health state."""
    await provider_state.ensure_loaded()
    order = providers_override if providers_override is not None else config.PROVIDER_ORDER

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

        for provider in order:
            if provider == "cookie":
                if provider_state.cookie_dead_since is not None:
                    known_bad_skipped.append("cookie")
                    continue
                has_api = bool(official_client.api_key_for(1) or official_client.api_key_for(2))
                try:
                    return await _attempt_cookie()
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Gọi Gemini (cookie) lỗi/treo lần 1, reset và thử lại 1 lần.",
                        exc_info=True,
                    )
                    await cookie_client.reset_client()
                    try:
                        return await _attempt_cookie()
                    except Exception as retry_exc:
                        last_exc = retry_exc
                        if not has_api:
                            raise
                        logger.warning(
                            "Cookie Gemini vẫn lỗi sau retry; chuyển provider.",
                            exc_info=True,
                        )
                        await provider_state.mark_cookie_dead()
            else:
                idx = 1 if provider == "api1" else 2
                if not official_client.api_key_for(idx):
                    continue
                if provider_state.api_in_cooldown(idx):
                    known_bad_skipped.append(provider)
                    continue
                try:
                    return await _attempt_api(idx)
                except Exception as exc:
                    if official_client.is_quota_exhausted_error(exc):
                        await provider_state.mark_api_exhausted(idx)
                        last_exc = exc
                        continue
                    logger.exception("%s lỗi (không phải hết quota).", provider)
                    last_exc = exc

        for provider in known_bad_skipped:
            try:
                if provider == "cookie":
                    return await _attempt_cookie()
                return await _attempt_api(1 if provider == "api1" else 2)
            except Exception as exc:
                last_exc = exc

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            "Không có provider nào khả dụng (cookie lỗi, chưa cấu hình API, "
            "hoặc API đang cooldown quota)."
        )


async def ask(
    prompt: str,
    model: Optional[str] = None,
    enable_search: bool = False,
    require_real_search: bool = False,
):
    """Run a one-turn task through the provider chain.

    ``require_real_search`` is a strict contract: it forces the official
    Google Search tool, excludes cookie, and raises when no official key exists.
    """
    providers_override = _search_only_providers() if require_real_search else None
    effective_prompt = (
        f"{_FORCED_SEARCH_DIRECTIVE}\n\n{prompt}" if require_real_search else prompt
    )
    model_name = model or await cookie_client.get_preferred_model_name()

    async def _cookie_call():
        client = await cookie_client.get_client()
        model_obj = await cookie_client.find_model(model_name) if model_name else None
        kwargs = {"model": model_obj} if model_obj else {}
        return await client.generate_content(effective_prompt, **kwargs)

    async def _api_call(idx: int):
        return await official_client.generate(
            idx,
            effective_prompt,
            model=model_name,
            enable_search=enable_search or require_real_search,
        )

    return await _run_provider_chain(
        cookie_call=_cookie_call,
        api_call=_api_call,
        providers_override=providers_override,
    )


async def chat(
    user_id: int,
    prompt: str,
    grounding: str = "",
    memory_context: str = "",
    require_real_search: bool = False,
):
    """Chat with shared history/memory through the provider chain."""
    full_prompt = prompt
    if grounding:
        full_prompt = f"{grounding}\n\n{full_prompt}"
    if memory_context:
        full_prompt = f"{memory_context}\n\n{full_prompt}"
    if require_real_search:
        full_prompt = f"{_FORCED_SEARCH_DIRECTIVE}\n\n{full_prompt}"

    async def _cookie_call():
        is_new_session = await cookie_client.ensure_chat_session()
        prompt_with_time = f"{official_client.now_vn_context()}\n{prompt}"
        if grounding:
            prompt_with_time = f"{grounding}\n\n{prompt_with_time}"
        if is_new_session and memory_context:
            prompt_with_time = f"{memory_context}\n\n{prompt_with_time}"
        if require_real_search:
            prompt_with_time = f"{_FORCED_SEARCH_DIRECTIVE}\n\n{prompt_with_time}"
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

    providers_override = _search_only_providers() if require_real_search else None
    return await _run_provider_chain(
        cookie_call=_cookie_call,
        api_call=_api_call,
        providers_override=providers_override,
    )


async def reset_chat() -> None:
    async with call_lock:
        cookie_client.clear_chat_session()


async def analyze_image(instruction: str, image_path: str):
    async def _cookie_call():
        client = await cookie_client.get_client()
        return await client.generate_content(instruction, files=[image_path])

    async def _api_call(idx: int):
        return await official_client.generate_image_prompt(idx, instruction, image_path)

    return await _run_provider_chain(cookie_call=_cookie_call, api_call=_api_call)


async def check_cookie_status() -> tuple[bool, str]:
    async def _ping():
        client = await cookie_client.get_client()
        await client.generate_content("ping")

    try:
        await _run_with_call_timeout(_ping)
        return True, "OK"
    except asyncio.TimeoutError:
        timeout_sec = _call_timeout_sec()
        logger.warning("Probe cookie quá %ss.", timeout_sec)
        return False, f"TimeoutError: ping cookie quá {timeout_sec}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def check_ai_studio_status(idx: int) -> tuple[bool, str]:
    return await official_client.check_ai_studio_status(idx)


async def try_cookie_now() -> tuple[bool, str]:
    await provider_state.ensure_loaded()
    async with call_lock:
        ok, detail = await check_cookie_status()
        if ok:
            await provider_state.mark_cookie_alive()
            await provider_state.set_active_provider("cookie")
        return ok, detail


_probe_task: Optional[asyncio.Task] = None


async def _cookie_probe_loop() -> None:
    while True:
        await asyncio.sleep(config.COOKIE_PROBE_INTERVAL_SEC)
        await provider_state.ensure_loaded()
        if provider_state.cookie_dead_since is None:
            continue
        try:
            async with call_lock:
                ok, _ = await check_cookie_status()
                if ok:
                    await provider_state.mark_cookie_alive()
                    await provider_state.set_active_provider("cookie")
        except Exception:
            logger.warning("Lỗi khi probe cookie nền.", exc_info=True)


def start_background_tasks() -> None:
    global _probe_task
    if _probe_task is None or _probe_task.done():
        _probe_task = asyncio.create_task(_cookie_probe_loop())
