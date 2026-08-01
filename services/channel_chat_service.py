"""Telegram-independent text/stock chat service.

The first Zalo vertical slice reuses the existing Gemini, stock, tools and
memory pipeline without constructing fake telegram.Update objects. Telegram
handlers can migrate to this service in a later change.
"""
import asyncio
from dataclasses import dataclass

import messages
import stock_analysis
from ai import orchestrator
from core import database as db
from services import memory_service, tools
from services.telemetry import telemetry


@dataclass(frozen=True)
class ChannelResult:
    messages: list[str]
    provider: str | None = None


_background_tasks: set[asyncio.Task] = set()


def split_for_zalo(text: str, limit: int = 1800) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _handle_stock(user_id: int, text: str) -> tuple[ChannelResult | None, str]:
    symbols = await stock_analysis.find_valid_symbols(text)
    if not symbols:
        if stock_analysis.looks_like_price_question(text):
            return ChannelResult([messages.STOCK_SYMBOL_UNRESOLVED]), ""
        return None, ""

    if stock_analysis.wants_portfolio_analysis(text, symbols):
        prompt_id = await telemetry.start(user_id, "portfolio_analysis", ",".join(symbols))
        try:
            output = await stock_analysis.analyze_portfolio(symbols, text, user_id=user_id)
            await telemetry.success(prompt_id, "portfolio_analysis", output)
            return ChannelResult([output]), ""
        except Exception as exc:
            await telemetry.failure(prompt_id, "portfolio_analysis", exc)
            return ChannelResult(["❌ Có lỗi khi soi danh mục, anh thử lại sau nhé."]), ""

    if stock_analysis.wants_full_analysis(text):
        outputs: list[str] = []
        for symbol in symbols:
            prompt_id = await telemetry.start(user_id, "stock_analysis", symbol)
            try:
                output = await stock_analysis.analyze_symbol(symbol, user_text=text, user_id=user_id)
                await telemetry.success(prompt_id, "stock_analysis", output)
                outputs.append(output)
            except Exception as exc:
                await telemetry.failure(prompt_id, "stock_analysis", exc)
                outputs.append(messages.STOCK_ANALYZE_FAILED.format(symbol=symbol))
        return ChannelResult(outputs), ""

    if stock_analysis.wants_price_quote(text, symbols):
        prompt_ids = [await telemetry.start(user_id, "stock_price", symbol) for symbol in symbols]
        results = await asyncio.gather(
            *(stock_analysis.quick_quote(symbol) for symbol in symbols),
            return_exceptions=True,
        )
        outputs: list[str] = []
        for symbol, prompt_id, result in zip(symbols, prompt_ids, results):
            if isinstance(result, BaseException):
                await telemetry.failure(prompt_id, "stock_price", result)
                outputs.append(messages.STOCK_QUOTE_FAILED.format(symbol=symbol))
            else:
                await telemetry.success(prompt_id, "stock_price", result)
                outputs.append(result)
        return ChannelResult(outputs), ""

    return None, await stock_analysis.build_price_grounding(symbols)


async def handle_channel_text(user_id: int, text: str) -> ChannelResult:
    text = (text or "").strip()
    if not text:
        return ChannelResult([])

    stock_result, grounding = await _handle_stock(user_id, text)
    if stock_result is not None:
        return stock_result

    prompt_id = await telemetry.start(user_id, "chat", text)
    try:
        tool_result = await tools.maybe_run_tool(user_id, text)
        combined_grounding = grounding
        if tool_result:
            combined_grounding = f"{grounding}\n\n{tool_result}" if grounding else tool_result

        memory_context = await memory_service.build_memory_context(user_id, query_text=text)
        response = await orchestrator.chat(
            user_id,
            text,
            grounding=combined_grounding,
            memory_context=memory_context,
            require_real_search=stock_analysis.wants_external_market_data(text),
        )
        reply = (response.text or "").strip()
        await telemetry.success(prompt_id, "chat", reply or "(không có nội dung)")
        if not reply:
            return ChannelResult([messages.CHAT_GENERIC_ERROR])

        await db.add_chat_message(user_id, "user", text)
        await db.add_chat_message(user_id, "model", reply)
        task = asyncio.create_task(memory_service.update_memory(user_id, text, reply))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        if getattr(response, "used_fallback", False):
            reply += "\n\n⚙️ API"
        provider = "api" if getattr(response, "used_fallback", False) else None
        return ChannelResult([reply], provider=provider)
    except Exception as exc:
        await telemetry.failure(prompt_id, "chat", exc)
        return ChannelResult(["❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau giây lát."])
