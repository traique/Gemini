"""Private Telegram control surface for the local Zalo gateway."""
import asyncio
import base64
from io import BytesIO
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from handlers import common

_CONTROL = "http://127.0.0.1:9901"

async def _json(method: str, path: str):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.request(method, _CONTROL + path)
        response.raise_for_status()
        return response.json()

@common.restricted
async def zalo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    try:
        status = await _json("GET", "/status")
        if status.get("connected"):
            await message.reply_text(f"✅ Zalo đã kết nối\nAccount ID: {status.get('accountId', 'không rõ')}\nGõ /nhomzalo từ tài khoản A để xem nhóm.")
            return
        await message.reply_text("⏳ Đang tạo QR đăng nhập Zalo B...")
        await _json("POST", "/login/qr")
        for _ in range(15):
            await asyncio.sleep(1)
            status = await _json("GET", "/status")
            qr = status.get("qr")
            if qr:
                image = base64.b64decode(qr.split(",", 1)[-1])
                await message.reply_photo(photo=BytesIO(image), caption="📷 Dùng tài khoản Zalo B quét QR này. Sau khi xác nhận, gõ /zalo lần nữa để kiểm tra trạng thái. Không chia sẻ QR này.")
                return
            if status.get("connected"):
                await message.reply_text("✅ Zalo đã đăng nhập thành công.")
                return
        await message.reply_text("❌ Chưa lấy được QR. Hãy thử lại /zalo.")
    except httpx.HTTPError:
        await message.reply_text("❌ Zalo gateway chưa sẵn sàng. Kiểm tra ZALO_ENABLED=true và log Render.")

@common.restricted
async def zalologout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _json("POST", "/logout")
        await update.effective_message.reply_text("🚪 Đã đăng xuất và xoá session Zalo đã lưu.")
    except httpx.HTTPError:
        await update.effective_message.reply_text("❌ Không kết nối được Zalo gateway.")
