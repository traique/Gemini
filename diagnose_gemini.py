"""
diagnose_gemini.py
-----------------------------------------

Script chẩn đoán Gemini WebAPI.

Chạy trên Render:

    https://your-app.onrender.com/diagnose  (header X-Diagnose-Token: <DIAGNOSE_SECRET>)

hoặc

    python diagnose_gemini.py

Dùng LẠI client chính (cookie_client.get_client()) thay vì tạo GeminiClient
riêng - tạo 1 phiên thứ hai cùng lúc với client chính sẽ làm cookie
__Secure-1PSIDTS bị xoay 2 nơi, dễ khiến 1 trong 2 phiên chết sớm và tốn
quota vô ích. Test cũng dùng text thay vì tạo ảnh, vì bot không còn tính
năng tạo ảnh (free tier API không hỗ trợ) và tạo ảnh test mỗi lần chẩn đoán
chỉ tốn quota mà không đại diện cho luồng thực tế của bot.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import traceback

from ai import cookie_client, orchestrator

try:
    from importlib.metadata import version

    GEMINI_WEBAPI_VERSION = version("gemini-webapi")
except Exception:
    GEMINI_WEBAPI_VERSION = "unknown"


LINE = "=" * 70


def title(text: str):
    print()
    print(LINE)
    print(text)
    print(LINE)


async def safe_list_models(client):
    """
    Tương thích nhiều version gemini-webapi.
    """

    if not hasattr(client, "list_models"):
        print("❌ Client không có list_models()")
        return None

    result = client.list_models()

    if inspect.isawaitable(result):
        return await result

    return result


async def main():

    psid = os.getenv("GEMINI_SECURE_1PSID", "").strip()
    psidts = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip()

    title("THÔNG TIN MÔI TRƯỜNG")

    print(f"gemini-webapi version : {GEMINI_WEBAPI_VERSION}")
    print()

    print(
        f"GEMINI_SECURE_1PSID   : {'OK' if psid else 'MISSING'}"
    )

    print(
        f"GEMINI_SECURE_1PSIDTS : {'OK' if psidts else 'MISSING'}"
    )

    print()

    if not psid:
        print("Không có cookie.")
        return

    title("KHỞI TẠO CLIENT (dùng lại client chính, không tạo phiên mới)")

    try:

        client = await cookie_client.get_client()

        print("✅ get_client() thành công (dùng chung phiên với bot)")

    except Exception as e:

        print("❌ get_client() thất bại")
        print(type(e).__name__)
        print(e)

        return

    title("LIST MODELS")

    try:

        models = await safe_list_models(client)

        if models is None:

            print("Không hỗ trợ list_models()")

        else:

            print(f"Số model: {len(models)}")

            for model in models:
                print(" -", model)

    except Exception:

        traceback.print_exc()

    title("TEST TEXT")

    prompt = "Trả lời ngắn gọn 1 câu: bạn đang hoạt động bình thường chứ?"

    try:

        response = await orchestrator.ask(prompt)

        print("Response class:", type(response))

        print()

        text = getattr(response, "text", "")

        print("TEXT")
        print("--------------------------------")

        if text:
            print(text)
        else:
            print("(empty)")

    except Exception:

        traceback.print_exc()

    title("DONE")


if __name__ == "__main__":
    asyncio.run(main())
