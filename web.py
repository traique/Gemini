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
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
import io
from contextlib import redirect_stdout

from diagnose_gemini import main as diagnose_main

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

# Giữ tham chiếu mạnh tới các background task xử lý update, để tránh bị
# garbage-collect giữa chừng (cạm bẫy kinh điển của asyncio.create_task khi
# không lưu lại task object ở đâu cả).
_background_tasks: set[asyncio.Task] = set()


def _build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("anh", handlers.image_cmd))
    app.add_handler(CommandHandler("reset", handlers.reset_chat_cmd))
    app.add_handler(CommandHandler("history", handlers.history_cmd))
    # Ảnh gửi vào -> tự động phân tích thành prompt (xem handlers.photo_msg)
    app.add_handler(MessageHandler(filters.PHOTO, handlers.photo_msg))
    # Tin nhắn thường (không phải lệnh /...) -> chat tự nhiên với Gemini.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.chat_msg))
    # Bắt mọi lệnh không khớp ở trên (phải add sau cùng)
    app.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_cmd))
    # Bắt mọi exception không được catch trong handler ở trên.
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

@api.get("/diagnose")
async def diagnose() -> Response:
    """
    Chạy diagnose_gemini.py ngay trên Render.
    Chỉ dùng để debug, xong nhớ xóa endpoint này.
    """

    buf = io.StringIO()

    try:
        with redirect_stdout(buf):
            await diagnose_main()
    except Exception as e:
        print(f"Lỗi ngoài dự kiến: {type(e).__name__}: {e}")

    return Response(
        content=buf.getvalue(),
        media_type="text/plain; charset=utf-8",
    )

@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    # Xác thực request thực sự đến từ Telegram (không phải ai đó đoán được URL).
    # Dùng hmac.compare_digest thay vì != để tránh timing attack dò secret
    # từng byte qua thời gian phản hồi.
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_SECRET):
        logger.warning("Webhook nhận request với secret token không khớp")
        return Response(status_code=403)

    if application is None:
        return Response(status_code=503)

    data = await request.json()
    update = Update.de_json(data, application.bot)

    # QUAN TRỌNG: KHÔNG await process_update() trực tiếp ở đây. Nếu handler
    # (vd /anh) bị treo lâu (chờ Gemini trả kết quả), HTTP response tới
    # Telegram sẽ không bao giờ được trả về -> Telegram coi là gửi thất bại
    # -> tự động gửi LẠI CHÍNH update đó sau vài phút -> bot xử lý lại từ
    # đầu -> nếu vẫn treo thì lại bị gửi lại tiếp -> lặp vô hạn.
    # Trả 200 ngay, xử lý update trong background task riêng để cắt đứt
    # vòng lặp retry của Telegram, bất kể handler bên dưới chạy bao lâu.
    task = asyncio.create_task(application.process_update(update))
    _background_tasks.add(task)

    def _on_task_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        # process_update() của PTB tự bắt lỗi qua add_error_handler, nhưng
        # phòng trường hợp có exception lọt ra ngoài (vd lỗi xảy ra TRƯỚC
        # khi PTB kịp dispatch vào error_handler), log lại để còn thấy trên
        # Render logs thay vì biến mất âm thầm.
        if not t.cancelled() and t.exception():
            logger.error("Background task xử lý update lỗi không bắt được:", exc_info=t.exception())

    task.add_done_callback(_on_task_done)

    return Response(status_code=200)
