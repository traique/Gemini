"""Các chuỗi thông báo gửi cho người dùng (Telegram) dùng chung nhiều nơi.

Gom về 1 chỗ để dễ sửa văn phong / dịch đa ngôn ngữ sau này, thay vì rải rác
literal string trong logic nghiệp vụ.
"""

# ─── Provider-chain (cookie <-> api1/api2) ──────────────────────────────────
COOKIE_DEAD_ALERT = (
    "⚠️ Cookie Gemini vừa lỗi, đã chuyển hẳn sang Google AI Studio API. "
    "Em sẽ tự thử lại cookie định kỳ, hoặc anh dán cookie mới rồi gõ /usecookie."
)
COOKIE_ALIVE_ALERT = "✅ Cookie Gemini đã hoạt động trở lại, em quay về dùng cookie nhé."
COOKIE_STALE_WARNING = (
    "⚠️ Cookie Gemini có dấu hiệu sắp hết hạn (__Secure-1PSIDTS "
    "không rotate trong một thời gian dài). Hãy kiểm tra/cập nhật cookie."
)

# ─── Phân tích cổ phiếu ──────────────────────────────────────────────────────
STOCK_FETCH_ERROR = "Em không lấy được dữ liệu giá cho mã {symbol} lúc này, anh thử lại sau ít phút nhé."
STOCK_ANALYZE_FAILED = "❌ Lỗi khi phân tích {symbol}, bỏ qua mã này."
STOCK_QUOTE_FAILED = "❌ Lỗi khi lấy giá {symbol}, bỏ qua mã này."

# ─── Chat & lệnh chung ───────────────────────────────────────────────────────
INVALID_COMMAND = "Lệnh không hợp lệ. Gõ /help để xem danh sách lệnh."
CHAT_GENERIC_ERROR = "Gemini không phản hồi gì, thử lại nhé."
PHOTO_TIMEOUT_ERROR = "❌ Tải ảnh từ Telegram bị timeout. Anh gửi lại ảnh hoặc thử ảnh nhỏ hơn nhé."
