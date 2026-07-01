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
    config.ensure_media_dir()
    asyncio.run(db.init_db())

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("anh", handlers.image_cmd))
    app.add_handler(CommandHandler("reset", handlers.reset_chat_cmd))
    app.add_handler(CommandHandler("history", handlers.history_cmd))
    # Ảnh gửi vào -> tự động phân tích thành prompt (xem handlers.photo_msg)
    app.add_handler(MessageHandler(filters.PHOTO, handlers.photo_msg))
    # Tin nhắn thường (không phải lệnh /...) -> chat tự nhiên với Gemini.
    # Phải add TRƯỚC handler catch-all filters.COMMAND ở dưới, vì 2 filter
    # này loại trừ nhau (COMMAND vs ~COMMAND) nên thực ra thứ tự không ảnh
    # hưởng tới việc match, nhưng đặt theo đúng luồng đọc cho dễ hiểu.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.chat_msg))
    # Bắt mọi lệnh không khớp ở trên (phải add sau cùng)
    app.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_cmd))
    # Bắt mọi exception không được catch trong handler ở trên - nếu thiếu,
    # lỗi sẽ chỉ bị log ra console và người dùng không nhận được phản hồi gì.
    app.add_error_handler(handlers.error_handler)

    logger.info("Bot đang khởi động (long polling, local)... Nhấn Ctrl+C để dừng.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
