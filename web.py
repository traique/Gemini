"""
Entrypoint dùng để DEPLOY LÊN RENDER (hoặc bất kỳ host yêu cầu Web Service).

Render free tier chỉ hỗ trợ Web Service (không có Background Worker free),
và Web Service free sẽ "ngủ" sau 15 phút không có traffic HTTP. Vì vậy bot
không dùng long-polling (xem main.py - chỉ dùng để chạy local) mà dùng
WEBHOOK: Telegram sẽ tự POST tới endpoint của mình mỗi khi có tin nhắn mới,
đúng kiểu traffic HTTP mà Render "Web Service" cần.

Chạy local để test webhook (cần ngrok hoặc tunnel để có HTTPS public URL):
    uvicorn web:api --host 0.0.0.0 --port 8000

Trên Render, start command:
    uvicorn web:api --host 0.0.0.0 --port $PORT
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update
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

application: Application | None = None


def _build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("anh", handlers.image_cmd))
    app.add_handler(CommandHandler("video", handlers.video_cmd))
    app.add_handler(CommandHandler("content", handlers.content_cmd))
    app.add_handler(CommandHandler("history", handlers.history_cmd))
    # Bắt mọi lệnh không khớp ở trên (phải add sau cùng)
    app.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_cmd))
    return app


@asynccontextmanager
async def lifespan(_: FastAPI):
    global application

    config.validate(require_webhook=True)
    await db.init_db()

    application = _build_application()
    await application.initialize()
    await application.start()

    webhook_url = f"{config.WEBHOOK_BASE_URL}{config.WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=config.WEBHOOK_SECRET,
        allowed_updates=["message"],
    )
    logger.info("Webhook đã set tới: %s", webhook_url)

    yield

    logger.info("Đang tắt bot...")
    await application.stop()
    await application.shutdown()
    await db.close_pool()


api = FastAPI(lifespan=lifespan)


@api.get("/")
async def health() -> dict:
    """
    Health check cho Render. Cũng là endpoint để cron-job.org (hoặc tương tự)
    ping định kỳ ~10 phút/lần nếu bạn muốn giảm khả năng service bị ngủ.
    Lưu ý: ping kiểu này KHÔNG đảm bảo chặn được sleep 100% trên free tier,
    chỉ giảm tần suất.
    """
    return {"status": "ok"}


@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    # Xác thực request thực sự đến từ Telegram (không phải ai đó đoán được URL)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if config.WEBHOOK_SECRET and secret != config.WEBHOOK_SECRET:
        logger.warning("Webhook nhận request với secret token không khớp")
        return Response(status_code=403)

    if application is None:
        return Response(status_code=503)

    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=200)
