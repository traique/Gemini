"""
Script chẩn đoán độc lập cho gemini-webapi.

QUAN TRỌNG: chạy script này TRỰC TIẾP TRÊN RENDER (qua tab "Shell" trong
dashboard service), KHÔNG chạy ở máy local. Lý do: vấn đề nghi ngờ là Google
đối xử khác nhau với IP server (Render, Singapore) so với IP bạn dùng để lấy
cookie (Việt Nam). Chạy ở local sẽ không tái hiện được vấn đề.

Cách chạy trên Render:
1. Vào Render Dashboard -> service của bạn -> tab "Shell"
2. python diagnose_gemini.py

Script không phụ thuộc handlers/db - chỉ cần GEMINI_SECURE_1PSID và
GEMINI_SECURE_1PSIDTS trong môi trường (đã có sẵn nếu chạy trên Render).
"""
import asyncio
import os

from gemini_webapi import GeminiClient


async def main() -> None:
    psid = os.getenv("GEMINI_SECURE_1PSID", "").strip()
    psidts = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip() or None

    print("=" * 60)
    print("KIỂM TRA BIẾN MÔI TRƯỜNG")
    print("=" * 60)
    print(f"GEMINI_SECURE_1PSID   : {'có (' + str(len(psid)) + ' ký tự)' if psid else 'THIẾU!'}")
    print(f"GEMINI_SECURE_1PSIDTS : {'có (' + str(len(psidts)) + ' ký tự)' if psidts else 'THIẾU - đây có thể là nguyên nhân!'}")
    print()

    if not psid:
        print("Không có 1PSID, dừng - không thể khởi tạo client.")
        return

    print("=" * 60)
    print("KHỞI TẠO CLIENT VÀ GỌI list_models() (RPC otAQ7b)")
    print("=" * 60)
    client = GeminiClient(psid, psidts)
    try:
        await client.init(timeout=60, auto_refresh=False)
    except Exception as e:
        print(f"❌ init() thất bại: {type(e).__name__}: {e}")
        print("-> Cookie gần như chắc chắn đã hết hạn/không hợp lệ.")
        return

    try:
        models = await client.list_models()
        print(f"✅ list_models() thành công, account thấy được {len(models)} model:")
        for m in models:
            print(f"   - {m}")
        print()
        print("Nếu lệnh này CHẠY ĐƯỢC nhưng /anh trong bot vẫn báo không tạo")
        print("được ảnh -> vấn đề nằm ở cách gọi generate_content/model được")
        print("chọn, KHÔNG phải cookie/IP. Báo lại kết quả này để debug tiếp.")
    except Exception as e:
        print(f"❌ list_models() thất bại: {type(e).__name__}: {e}")
        print()
        print("Nếu thông báo lỗi có nhắc tới UNAUTHENTICATED -> cookie hết hạn,")
        print("lấy cookie mới từ gemini.google.com (private/incognito window).")
        print("Nếu nhắc tới LOCATION_REJECTED -> đúng là IP server (Singapore)")
        print("đang bị Google chặn cho các tool nâng cao; cân nhắc đổi region")
        print("Render (xem comment trong render.yaml) hoặc chấp nhận hạn chế này.")

    print()
    print("=" * 60)
    print("THỬ GỌI generate_content TRỰC TIẾP TỪ SERVER NÀY")
    print("=" * 60)
    try:
        response = await client.generate_content("Generate an image: a cute cat drinking milk tea")
        print(f"text: {(response.text or '')[:300]}")
        print(f"số ảnh trả về: {len(response.images)}")
    except Exception as e:
        print(f"❌ generate_content thất bại: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
