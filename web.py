"""Entrypoint dùng để DEPLOY LÊN RENDER (Web Service, dùng webhook thay vì long polling).

Local test webhook: uvicorn web:api --host 0.0.0.0 --port 8000 (cần ngrok/tunnel).
Trên Render: uvicorn web:api --host 0.0.0.0 --port $PORT
"""
import asyncio
import hmac
import io
import logging
from contextlib import asynccontextmanager, redirect_stdout

from diagnose_gemini import main as diagnose_main
from fastapi import FastAPI, Request, Response
from telegram import Update
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

application: Application | None = None

# Giữ tham chiếu mạnh tới background task xử lý update, tránh bị garbage-collect giữa chừng.
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()


def _build_application() -> Application:
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
    return app


@asynccontextmanager
async def lifespan(_: FastAPI):
    global application

    config.validate(require_webhook=True)
    config.ensure_media_dir()
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
    await stock_providers.close_http_client()
    await db.close_pool()


api = FastAPI(lifespan=lifespan)


@api.get("/")
async def health() -> dict:
    return {"status": "ok"}


@api.get(config.DIAGNOSE_PATH)
async def diagnose(request: Request) -> Response:
    """Debug only - yêu cầu ?token=<WEBHOOK_SECRET>, không public."""
    token = request.query_params.get("token", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(token, config.WEBHOOK_SECRET):
        return Response(status_code=403)

    async with _diagnose_lock:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                await diagnose_main()
        except Exception as e:
            print(f"Lỗi ngoài dự kiến: {type(e).__name__}: {e}")
        return Response(content=buf.getvalue(), media_type="text/plain; charset=utf-8")


@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_SECRET):
        logger.warning("Webhook nhận request với secret token không khớp")
        return Response(status_code=403)

    if application is None:
        return Response(status_code=503)

    data = await request.json()
    update = Update.de_json(data, application.bot)

    # Trả 200 ngay, xử lý update trong background task để không chặn HTTP response
    # (nếu chặn, Telegram coi là gửi thất bại và gửi lại update, gây lặp vô hạn).
    task = asyncio.create_task(application.process_update(update))
    _background_tasks.add(task)

    def _on_task_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error("Background task xử lý update lỗi không bắt được:", exc_info=t.exception())

    task.add_done_callback(_on_task_done)

    return Response(status_code=200)
