"""Private Telegram controls for Zalo login and controller pairing."""
import asyncio,base64
from io import BytesIO
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from handlers import common
_CONTROL="http://127.0.0.1:9901"
async def _json(method,path):
 async with httpx.AsyncClient(timeout=10)as client:
  r=await client.request(method,_CONTROL+path);r.raise_for_status();return r.json()
@common.restricted
async def zalo_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
 m=update.effective_message
 try:
  s=await _json("GET","/status")
  if s.get("connected"):
   if s.get("controllerPaired"):
    await m.reply_text(f"✅ Zalo đã kết nối và ghép đôi A\nAccount B: {s.get('accountId','không rõ')}")
   else:
    p=await _json("POST","/pairing/start");code=p["code"]
    await m.reply_text(f"🔐 Mã ghép đôi: {code}\n\nTừ tài khoản Zalo A, nhắn riêng cho B đúng nội dung:\n/pair {code}\n\nMã hết hạn sau 5 phút và chỉ dùng một lần.")
   return
  await m.reply_text("⏳ Đang tạo QR đăng nhập Zalo B...");await _json("POST","/login/qr")
  for _ in range(15):
   await asyncio.sleep(1);s=await _json("GET","/status");qr=s.get("qr")
   if qr:
    await m.reply_photo(photo=BytesIO(base64.b64decode(qr.split(",",1)[-1])),caption="📷 Quét bằng Zalo B. Xác nhận xong, gửi lại /zalo để lấy mã ghép đôi A.");return
   if s.get("connected"):await m.reply_text("✅ B đã đăng nhập. Gửi lại /zalo để ghép đôi A.");return
  await m.reply_text("❌ Chưa lấy được QR. Hãy thử lại /zalo.")
 except httpx.HTTPError:await m.reply_text("❌ Gateway chưa sẵn sàng. Kiểm tra ZALO_ENABLED=true và log Render.")
@common.restricted
async def zalologout_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
 try:await _json("POST","/logout");await update.effective_message.reply_text("🚪 Đã đăng xuất B và xoá liên kết A.")
 except httpx.HTTPError:await update.effective_message.reply_text("❌ Không kết nối được gateway.")
