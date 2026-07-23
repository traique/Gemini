"""Factory đăng ký handler dùng chung cho cả main.py (long polling) và
web.py (webhook), để tránh copy-paste và lệch cấu hình giữa 2 entrypoint."""
import logging

from telegram import BotCommand
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import scheduler
import stock_providers
import tg_format
from ai import orchestrator
from core import config, database as db
from handlers import chat_router, commands, media_handler

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "Bắt đầu, giới thiệu Lan Anh"),
    BotCommand("help", "Xem hướng dẫn đầy đủ"),
    BotCommand("prompt", "Viết prompt tạo ảnh từ mô tả cơ bản"),
    BotCommand("gia", "Tìm và so sánh giá sản phẩm (iPhone, Tivi...)"),
    BotCommand("reset", "Xoá ngữ cảnh chat, bắt đầu hội thoại mới"),
    BotCommand("history", "Xem 10 lượt gần nhất"),
    BotCommand("memory", "Xem trí nhớ dài hạn (sự thật + tóm tắt)"),
    BotCommand("forget", "Xoá trí nhớ dài hạn"),
    BotCommand("notes", "Xem ghi chú đã lưu"),
    BotCommand("model", "Xem/đổi model chat (vd /model pro)"),
    BotCommand("status", "Xem provider đang dùng (cookie/api1/api2)"),
    BotCommand("usecookie", "Ép thử lại cookie ngay"),
]

async def _post_init(app: Application) -> None:
    await db.init_db()
    await app.bot.set_my_commands(COMMANDS)
    await orchestrator.init_provider_state()
    orchestrator.start_background_tasks()
    scheduler.start(config.ALLOWED_USER_ID)

async def _post_shutdown(app: Application) -> None:
    await stock_providers.close_http_client()
    await db.close_pool()

def build_application() -> Application:
    request = HTTPXRequest(
        connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=config.TELEGRAM_READ_TIMEOUT,
        write_timeout=config.TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=config.TELEGRAM_POOL_TIMEOUT,
    )
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", commands.start_cmd))
    app.add_handler(CommandHandler("help", commands.help_cmd))
    app.add_handler(CommandHandler("prompt", commands.prompt_cmd))
    app.add_handler(CommandHandler("gia", commands.price_cmd))
    app.add_handler(CommandHandler("reset", commands.reset_chat_cmd))
    app.add_handler(CommandHandler("history", commands.history_cmd))
    app.add_handler(CommandHandler("memory", commands.memory_cmd))
    app.add_handler(CommandHandler("forget", commands.forget_cmd))
    app.add_handler(CommandHandler("notes", commands.notes_cmd))
    app.add_handler(CommandHandler("model", commands.model_cmd))
    app.add_handler(CommandHandler("status", commands.status_cmd))
    app.add_handler(CommandHandler("usecookie", commands.usecookie_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, media_handler.photo_msg))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_router.chat_msg))
    app.add_handler(MessageHandler(filters.COMMAND, commands.unknown_cmd))
    app.add_error_handler(commands.error_handler)

    async def _alert_allowed_user(text: str) -> None:
        try:
            await app.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text)
        except Exception:
            logger.warning("Không gửi được cảnh báo cookie qua Telegram.", exc_info=True)

    orchestrator.set_alert_callback(_alert_allowed_user)

    async def _notify_user(user_id: int, text: str) -> None:
        try:
            await tg_format.send_rich(app.bot, user_id, text)
        except Exception:
            logger.warning("Không gửi được thông báo chủ động (reminder/digest) qua Telegram.", exc_info=True)

    scheduler.set_notify_callback(_notify_user)

    return app
