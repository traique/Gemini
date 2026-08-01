"""Entrypoint dùng để DEPLOY LÊN RENDER (Web Service, dùng webhook thay vì long polling).

Local test webhook: uvicorn web:api --host 0.0.0.0 --port 8000 (cần ngrok/tunnel).
Trên Render: uvicorn web:api --host 0.0.0.0 --port $PORT
"""
import asyncio
import collections
import hmac
import io
import logging
from contextlib import asynccontextmanager, redirect_stdout

from diagnose_gemini import main as diagnose_main
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application

import bot_app
import logging_setup
from channels.router import router as zalo_router
from core import config

logging_setup.configure_logging()
logger = logging.getLogger(__name__)

application: Application | None = None
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()
_seen_updates: "collections.OrderedDict[int, None]" = collections.OrderedDict()
_SEEN_MAX = 1000


def _already_seen(update_id: int) -> bool:
    if update_id in _seen_updates:
        return True
    _seen_updates[update_id] = None
    if len(_seen_updates) > _SEEN_MAX:
        _seen_updates.popitem(last=False)
    return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global application
    config.validate(require_webhook=True)
    config.ensure_media_dir()
    application = bot_app.build_application()
    await application.initialize()
    await bot_app._post_init(application)
    await application.start()

    webhook_url = config.WEBHOOK_BASE_URL.rstrip("/") + config.WEBHOOK_PATH
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


api = FastAPI(lifespan=lifespan)
api.include_router(zalo_router)


@api.api_route("/", methods=["GET", "HEAD"])
async def health() -> dict:
    return {"status": "ok"}


@api.get(config.DIAGNOSE_PATH)
async def diagnose(request: Request) -> Response:
    token = request.headers.get("X-Diagnose-Token", "")
    if not config.DIAGNOSE_SECRET or not hmac.compare_digest(token, config.DIAGNOSE_SECRET):
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
    if update.update_id is not None and _already_seen(update.update_id):
        return Response(status_code=200)

    task = asyncio.create_task(application.process_update(update))
    _background_tasks.add(task)

    def _on_task_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        try:
            t.result()
        except Exception:
            logger.exception("Background task xử lý update lỗi không bắt được")

    task.add_done_callback(_on_task_done)
    return Response(status_code=200)
