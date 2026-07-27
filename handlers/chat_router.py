"""Điểm vào cho mọi tin nhắn văn bản thường (không phải lệnh /). Quyết định
forward sang handlers/stock_handler.py (nhận diện mã cổ phiếu) hay gửi thẳng
cho ai.orchestrator.chat() (chat tự nhiên, persona Lan Anh)."""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai import orchestrator
from core import database as db
from handlers import common, stock_handler
from services import memory_service, price_service, tools
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

# Giữ tham chiếu mạnh tới task cập nhật trí nhớ dài hạn chạy ngầm sau mỗi lượt
# chat thành công, tránh bị garbage-collect giữa chừng (task không được await
# trực tiếp trong luồng trả lời, để không làm chậm phản hồi cho người dùng).
_background_tasks: set[asyncio.Task] = set()


@common.restricted
async def chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    if price_service.is_product_price_query(text):
        await _handle_product_price_query(update, user_id, text)
        return

    route = await stock_handler.maybe_handle(update, user_id, text)
    if route.handled:
        return
    grounding = route.grounding

    prompt_id = await telemetry.start(user_id, "chat", text)
    try:
        tool_result = await tools.maybe_run_tool(user_id, text)
        combined_grounding = grounding
        if tool_result:
            combined_grounding = f"{grounding}\n\n{tool_result}" if grounding else tool_result

        memory_context = await memory_service.build_memory_context(user_id, query_text=text)
        response = await orchestrator.chat(
            user_id, text, grounding=combined_grounding, memory_context=memory_context
        )
        reply_text = (response.text or "").strip()

        await telemetry.success(prompt_id, "chat", reply_text or "(không có nội dung)")
        if reply_text:
            await db.add_chat_message(user_id, "user", text)
            await db.add_chat_message(user_id, "model", reply_text)
            reply_out = reply_text
            mem_task = asyncio.create_task(memory_service.update_memory(user_id, text, reply_text))
            _background_tasks.add(mem_task)
            mem_task.add_done_callback(_background_tasks.discard)
        else:
            reply_out = ""
        if reply_out and getattr(response, "used_fallback", False):
            reply_out += "\n\n⚙️ API"
        await common.reply_long_text(
            update.message, reply_out or messages.CHAT_GENERIC_ERROR
        )
    except Exception as e:
        logger.exception("Lỗi chat tự nhiên")
        await telemetry.failure(prompt_id, "chat", e)
        await update.message.reply_text(
            "❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau giây lát."
        )


async def _handle_product_price_query(update: Update, user_id: int, text: str) -> None:
    product_query = price_service.extract_product_query_from_text(text)
    if not product_query:
        return
    prompt_id = await telemetry.start(user_id, "price_search", product_query)
    try:
        result_text = await price_service.fetch_price_message(product_query)
        await telemetry.success(prompt_id, "price_search", result_text)
        await common.reply_long_text(update.message, result_text)
    except price_service.PriceServiceError as e:
        logger.warning("chat_msg: không lấy được giá cho '%s': %s", product_query, e)
        await telemetry.failure(prompt_id, "price_search", e)
        await update.message.reply_text("Em không tìm được giá đáng tin lúc này, anh thử lại sau nha.")
    except Exception as e:
        logger.exception("Lỗi tìm giá sản phẩm (hỏi tự nhiên)")
        await telemetry.failure(prompt_id, "price_search", e)
        await update.message.reply_text("❌ Có lỗi khi tìm giá. Hãy thử lại sau giây lát.")
