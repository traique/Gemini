"""Entrypoint chạy LOCAL bằng long polling - test nhanh, không cần webhook/HTTPS.
Deploy lên Render thì dùng web.py (webhook) thay vì file này.
"""
import asyncio
import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

import config
import db
import handlers
import stock_providers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def main() -> None:
    config.validate()
    config.ensure_media_dir()
    asyncio.run(db.init_db())

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("anh", handlers.image_cmd))
    app.add_handler(CommandHandler("reset", handlers.reset_chat_cmd))
    app.add_handler(CommandHandler("history", handlers.history_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.photo_msg))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.chat_msg))
    app.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_cmd))
    app.add_error_handler(handlers.error_handler)

    try:
        logger.info("Bot đang khởi động (long polling, local)... Nhấn Ctrl+C để dừng.")
        app.run_polling(allowed_updates=["message"])
    finally:
        asyncio.run(stock_providers.close_http_client())
        asyncio.run(db.close_pool())


if __name__ == "__main__":
    main()
