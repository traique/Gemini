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
from core import config

logging_setup.configure_logging()
logger = logging.getLogger(__name__)

application: Application | None = None

# Giữ tham chiếu mạnh tới background task xử lý update, tránh bị garbage-collect giữa chừng.
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()

# Chống xử lý trùng update_id: Telegram có thể gửi lại cùng 1 update (cold
# start, set lại webhook,...) trong khi mình đã ack 200 và xử lý background.
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
    # application.initialize()/start() KHÔNG tự gọi post_init - PTB chỉ tự
    # invoke post_init bên trong run_polling()/run_webhook() (main.py dùng
    # run_polling nên không cần dòng này, nhưng web.py tự quản lý lifecycle
    # qua initialize()/start() nên phải gọi tay, nếu không toàn bộ background
    # task đăng ký trong _post_init - probe cookie, reminder, daily digest,
    # KHỞI TẠO DB - không bao giờ chạy khi deploy qua webhook).
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
    # application.shutdown() tự gọi post_shutdown (đã đăng ký trong
    # bot_app.build_application() qua .post_shutdown()) - đóng DB pool +
    # HTTP client trong cùng Event Loop, không cần lặp lại ở đây.
    await application.shutdown()


api = FastAPI(lifespan=lifespan)


@api.api_route("/", methods=["GET", "HEAD"])
async def health() -> dict:
    return {"status": "ok"}


@api.get(config.DIAGNOSE_PATH)
async def diagnose(request: Request) -> Response:
    """Debug only - yêu cầu header X-Diagnose-Token: <DIAGNOSE_SECRET>, không public.
    Dùng header thay vì query string (?token=...) để tránh lộ token qua access
    log / proxy log / Referer header."""
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

    # Trả 200 ngay, xử lý update trong background task để không chặn HTTP response
    # (nếu chặn, Telegram coi là gửi thất bại và gửi lại update, gây lặp vô hạn).
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
