"""
Entrypoint chạy LOCAL bằng long polling - tiện để test nhanh trên máy bạn,
KHÔNG cần webhook/HTTPS public URL.

Khi deploy lên Render, dùng web.py (webhook) thay vì file này - xem README.
"""
import asyncio
import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

import config
import db
import handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def main() -> None:
    config.validate()
    asyncio.run(db.init_db())

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("anh", handlers.image_cmd))
    app.add_handler(CommandHandler("video", handlers.video_cmd))
    app.add_handler(CommandHandler("content", handlers.content_cmd))
    app.add_handler(CommandHandler("history", handlers.history_cmd))
    # Bắt mọi lệnh không khớp ở trên (phải add sau cùng)
    app.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_cmd))

    logger.info("Bot đang khởi động (long polling, local)... Nhấn Ctrl+C để dừng.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
