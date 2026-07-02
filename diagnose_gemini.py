"""
diagnose_gemini.py
-----------------------------------------

Script chẩn đoán Gemini WebAPI.

Chạy trên Render:

    https://your-app.onrender.com/diagnose

hoặc

    python diagnose_gemini.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import traceback

from gemini_webapi import GeminiClient

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

    title("KHỞI TẠO CLIENT")

    client = GeminiClient(
        psid,
        psidts or None,
    )

    try:

        await client.init(
            timeout=60,
            auto_refresh=False,
        )

        print("✅ init() thành công")

    except Exception as e:

        print("❌ init() thất bại")
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

    title("TEST IMAGE")

    prompt = (
        "Create a beautiful realistic image of "
        "a cute orange cat drinking bubble milk tea. "
        "Return image."
    )

    try:

        response = await client.generate_content(prompt)

        print("Response class:", type(response))

        print()

        text = getattr(response, "text", "")

        print("TEXT")
        print("--------------------------------")

        if text:
            print(text)
        else:
            print("(empty)")

        print()

        images = getattr(response, "images", [])

        videos = getattr(response, "videos", [])

        print("Images :", len(images))
        print("Videos :", len(videos))

        if images:

            print()

            print("Image types:")

            for img in images:
                print(type(img))

    except Exception:

        traceback.print_exc()

    title("DONE")


if __name__ == "__main__":
    asyncio.run(main())
