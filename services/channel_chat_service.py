"""Telegram-independent text, command and stock chat service."""
import asyncio
from dataclasses import dataclass

import messages
import stock_analysis
from ai import orchestrator
from core import database as db
from services import memory_service, tools
from services.channel_command_service import maybe_handle_command
from services.telemetry import telemetry


@dataclass(frozen=True)
class ChannelResult:
    messages: list[str]
    provider: str | None = None


_background_tasks: set[asyncio.Task] = set()


def split_for_zalo(text: str, limit: int = 1800) -> list[str]:
    text = (text or "").strip()
    if not text: return []
    if len(text) <= limit: return [text]
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2: cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2: cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0: cut = limit
        chunks.append(remaining[:cut].strip()); remaining = remaining[cut:].strip()
    if remaining: chunks.append(remaining)
    return chunks


async def _handle_stock(user_id: int, text: str) -> tuple[ChannelResult | None, str]:
    symbols = await stock_analysis.find_valid_symbols(text)
    if not symbols:
        if stock_analysis.looks_like_price_question(text): return ChannelResult([messages.STOCK_SYMBOL_UNRESOLVED]), ""
        return None, ""
    if stock_analysis.wants_portfolio_analysis(text, symbols):
        try: return ChannelResult([await stock_analysis.analyze_portfolio(symbols, text, user_id=user_id)]), ""
        except Exception: return ChannelResult(["❌ Có lỗi khi soi danh mục."]), ""
    if stock_analysis.wants_full_analysis(text):
        outputs = []
        for symbol in symbols:
            try: outputs.append(await stock_analysis.analyze_symbol(symbol, user_text=text, user_id=user_id))
            except Exception: outputs.append(messages.STOCK_ANALYZE_FAILED.format(symbol=symbol))
        return ChannelResult(outputs), ""
    if stock_analysis.wants_price_quote(text, symbols):
        results = await asyncio.gather(*(stock_analysis.quick_quote(s) for s in symbols), return_exceptions=True)
        return ChannelResult([messages.STOCK_QUOTE_FAILED.format(symbol=s) if isinstance(r, BaseException) else r for s, r in zip(symbols, results)]), ""
    return None, await stock_analysis.build_price_grounding(symbols)


async def handle_channel_text(user_id: int, text: str) -> ChannelResult:
    text = (text or "").strip()
    if not text: return ChannelResult([])
    command_result = await maybe_handle_command(user_id, text)
    if command_result is not None:
        outputs, provider = command_result
        return ChannelResult(outputs, provider)
    stock_result, grounding = await _handle_stock(user_id, text)
    if stock_result is not None: return stock_result
    prompt_id = await telemetry.start(user_id, "chat", text)
    try:
        tool_result = await tools.maybe_run_tool(user_id, text)
        combined = f"{grounding}\n\n{tool_result}" if grounding and tool_result else (tool_result or grounding)
        memory = await memory_service.build_memory_context(user_id, query_text=text)
        response = await orchestrator.chat(user_id, text, grounding=combined, memory_context=memory, require_real_search=stock_analysis.wants_external_market_data(text))
        reply = (response.text or "").strip()
        await telemetry.success(prompt_id, "chat", reply or "(không có nội dung)")
        if not reply: return ChannelResult([messages.CHAT_GENERIC_ERROR])
        await db.add_chat_message(user_id, "user", text); await db.add_chat_message(user_id, "model", reply)
        task = asyncio.create_task(memory_service.update_memory(user_id, text, reply)); _background_tasks.add(task); task.add_done_callback(_background_tasks.discard)
        fallback = bool(getattr(response, "used_fallback", False))
        return ChannelResult([reply + ("\n\n⚙️ API" if fallback else "")], "api" if fallback else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "chat", exc)
        return ChannelResult(["❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau."])
