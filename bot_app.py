"""Factory đăng ký handler dùng chung cho long polling và webhook."""
import logging
from telegram import BotCommand
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import scheduler, stock_providers, tg_format
from ai import orchestrator
from core import config, database as db
from handlers import chat_router, commands, media_handler, zalo_login
logger = logging.getLogger(__name__)
COMMANDS = [
 BotCommand("start", "Bắt đầu"), BotCommand("help", "Xem hướng dẫn"),
 BotCommand("zalo", "Đăng nhập hoặc xem trạng thái Zalo B"), BotCommand("zalologout", "Đăng xuất Zalo B"),
 BotCommand("prompt", "Viết prompt tạo ảnh"), BotCommand("gia", "Tìm giá sản phẩm"),
 BotCommand("reset", "Xoá ngữ cảnh chat"), BotCommand("history", "Xem lịch sử"),
 BotCommand("memory", "Xem trí nhớ dài hạn"), BotCommand("forget", "Xoá trí nhớ"),
 BotCommand("notes", "Xem ghi chú"), BotCommand("model", "Xem/đổi model"),
 BotCommand("status", "Xem provider"), BotCommand("usecookie", "Thử lại cookie"),
]
async def _post_init(app):
 await db.init_db(); await app.bot.set_my_commands(COMMANDS); await orchestrator.init_provider_state(); orchestrator.start_background_tasks(); scheduler.start(config.ALLOWED_USER_ID)
async def _post_shutdown(app): await stock_providers.close_http_client(); await db.close_pool()
def build_application():
 request = HTTPXRequest(connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT, read_timeout=config.TELEGRAM_READ_TIMEOUT, write_timeout=config.TELEGRAM_WRITE_TIMEOUT, pool_timeout=config.TELEGRAM_POOL_TIMEOUT)
 app = Application.builder().token(config.TELEGRAM_TOKEN).request(request).post_init(_post_init).post_shutdown(_post_shutdown).build()
 for name, handler in [("start",commands.start_cmd),("help",commands.help_cmd),("zalo",zalo_login.zalo_cmd),("zalologout",zalo_login.zalologout_cmd),("prompt",commands.prompt_cmd),("gia",commands.price_cmd),("reset",commands.reset_chat_cmd),("history",commands.history_cmd),("memory",commands.memory_cmd),("forget",commands.forget_cmd),("notes",commands.notes_cmd),("model",commands.model_cmd),("status",commands.status_cmd),("usecookie",commands.usecookie_cmd)]: app.add_handler(CommandHandler(name, handler))
 app.add_handler(MessageHandler(filters.PHOTO, media_handler.photo_msg)); app.add_handler(MessageHandler(filters.Document.IMAGE, media_handler.photo_msg)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_router.chat_msg)); app.add_handler(MessageHandler(filters.COMMAND, commands.unknown_cmd)); app.add_error_handler(commands.error_handler)
 async def alert(text):
  try: await app.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text)
  except Exception: logger.warning("Không gửi được cảnh báo.", exc_info=True)
 orchestrator.set_alert_callback(alert)
 async def notify(uid,text):
  try: await tg_format.send_rich(app.bot, uid, text)
  except Exception: logger.warning("Không gửi được thông báo.", exc_info=True)
 scheduler.set_notify_callback(notify); return app
