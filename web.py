"""Entrypoint dùng để DEPLOY LÊN RENDER (Web Service, dùng webhook thay vì long polling)."""
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
from channels import zalo_scheduler
from core import config
from services.background_tasks import stop_tracked_tasks

logging_setup.configure_logging()
logger = logging.getLogger(__name__)
application: Application | None = None
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()
_seen_updates: "collections.OrderedDict[int, None]" = collections.OrderedDict()
_SEEN_MAX = 1000


async def _stop_webhook_tasks() -> None:
    """Drain request handlers, rồi huỷ lượt treo trước khi đóng app/DB."""
    await stop_tracked_tasks(
        _background_tasks,
        timeout=30.0,
        logger=logger,
        label="Telegram webhook",
    )


async def _safe_shutdown(label: str, awaitable) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Shutdown step lỗi: %s", label)


def _already_seen(update_id: int) -> bool:
    if update_id in _seen_updates: return True
    _seen_updates[update_id] = None
    if len(_seen_updates) > _SEEN_MAX: _seen_updates.popitem(last=False)
    return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global application
    config.validate(require_webhook=True)
    config.ensure_media_dir()
    application = bot_app.build_application()
    initialized = False
    app_started = False
    app_resources_started = False
    try:
        await application.initialize()
        initialized = True
        # Cleanup functions đều idempotent; đánh dấu trước để vẫn dọn được
        # phần tài nguyên đã khởi tạo nếu _post_init lỗi giữa chừng.
        app_resources_started = True
        await bot_app._post_init(application)
        await application.start()
        app_started = True
        webhook_url = config.WEBHOOK_BASE_URL.rstrip("/") + config.WEBHOOK_PATH
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=["message"],
        )
        logger.info("Webhook đã set tới: %s", webhook_url)
        zalo_scheduler.start()
        yield
    finally:
        logger.info("Đang tắt bot...")
        await _safe_shutdown("Zalo scheduler", zalo_scheduler.stop())
        await _safe_shutdown("webhook tasks", _stop_webhook_tasks())
        if app_started:
            await _safe_shutdown("Telegram application stop", application.stop())
        # Khi tự quản lý lifecycle FastAPI, Application.shutdown() không gọi
        # callback post_shutdown đã đăng ký ở builder; phải gọi cleanup rõ ràng.
        if app_resources_started:
            await _safe_shutdown("application resources", bot_app._post_shutdown(application))
        if initialized:
            await _safe_shutdown("Telegram application shutdown", application.shutdown())
        application = None


api = FastAPI(lifespan=lifespan)
api.include_router(zalo_router)


@api.api_route("/", methods=["GET", "HEAD"])
async def health() -> dict: return {"status": "ok"}


@api.get(config.DIAGNOSE_PATH)
async def diagnose(request: Request) -> Response:
    token = request.headers.get("X-Diagnose-Token", "")
    if not config.DIAGNOSE_SECRET or not hmac.compare_digest(token, config.DIAGNOSE_SECRET): return Response(status_code=403)
    async with _diagnose_lock:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf): await diagnose_main()
        except Exception as e: print(f"Lỗi ngoài dự kiến: {type(e).__name__}: {e}")
        return Response(content=buf.getvalue(), media_type="text/plain; charset=utf-8")


@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_SECRET): return Response(status_code=403)
    if application is None: return Response(status_code=503)
    update = Update.de_json(await request.json(), application.bot)
    if update.update_id is not None and _already_seen(update.update_id): return Response(status_code=200)
    task = asyncio.create_task(application.process_update(update))
    _background_tasks.add(task)
    def done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            try: t.result()
            except Exception: logger.exception("Background task xử lý update lỗi không bắt được")
    task.add_done_callback(done)
    return Response(status_code=200)
