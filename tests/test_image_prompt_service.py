from services.image_prompt_service import (
    build_image_to_prompt_instruction,
    build_text_to_image_instruction,
    format_telegram_html,
    response_text,
    wants_identity_lock,
    wants_max_realism,
)


def test_wants_identity_lock_vi():
    assert wants_identity_lock("giữ mặt giúp anh")


def test_wants_identity_lock_en():
    assert wants_identity_lock("preserve face from reference")


def test_wants_identity_lock_su_dung_khuon_mat():
    assert wants_identity_lock("sử dụng khuôn mặt của tôi")
    assert wants_identity_lock("giữ khuôn mặt")
    assert wants_identity_lock("dùng khuôn mặt của em nha")


def test_wants_identity_lock_none():
    assert not wants_identity_lock(None)
    assert not wants_identity_lock("")
    assert not wants_identity_lock("robot pha cà phê ở Đà Lạt")


def test_wants_max_realism_vi():
    assert wants_max_realism("chân thật như ảnh chụp")


def test_wants_max_realism_none():
    assert not wants_max_realism(None)
    assert not wants_max_realism("")


def test_text_instruction_no_midjourney():
    s = build_text_to_image_instruction("robot pha cà phê")
    assert "Do NOT use --ar" in s
    assert "Prompt cho Gemini" in s
    assert "Prompt cho ChatGPT" in s
    assert "Midjourney" in s
    assert "seed" in s.lower()


def test_text_instruction_no_forced_identity_by_default():
    s = build_text_to_image_instruction("robot pha cà phê trong quán nhỏ ở Đà Lạt")
    assert "20-year-old" not in s
    assert "identity-lock" in s.lower() or "identity lock" in s.lower()


def test_text_instruction_identity_when_requested():
    s = build_text_to_image_instruction("giữ mặt, đổi background")
    assert "preserve the face" in s.lower() or "preserve facial features" in s.lower()


def test_text_instruction_realism_when_requested():
    s = build_text_to_image_instruction(
        "cô gái ngồi quán cà phê ngày mưa, chân thật như ảnh chụp, có hồn"
    )
    assert "candid" in s.lower()
    assert "visible pores" in s.lower()
    assert "masterpiece" in s.lower()  # xuất hiện trong danh sách từ CẤM


def test_image_instruction_has_analysis_structure():
    s = build_image_to_prompt_instruction("chân thật nhất")
    assert "Phân tích ảnh" in s
    assert "Prompt cho Gemini" in s
    assert "Prompt cho ChatGPT" in s


def test_image_instruction_no_identity_by_default():
    s = build_image_to_prompt_instruction(None)
    assert "identity-lock" in s.lower()
    assert "not necessarily a portrait" in s.lower()


def test_image_instruction_identity_when_caption_asks():
    s = build_image_to_prompt_instruction("giữ mặt, đổi sang bối cảnh quán cà phê mưa")
    assert "preserve the face" in s.lower() or "preserve facial features" in s.lower()


def test_response_text_strips_and_handles_missing():
    class FakeResponse:
        text = "  hello world  "

    assert response_text(FakeResponse()) == "hello world"

    class NoTextResponse:
        pass

    assert response_text(NoTextResponse()) == ""
    assert response_text(None) == ""


def test_format_telegram_html_wraps_each_prompt_in_own_pre():
    raw = (
        "🎨 Prompt cho Gemini:\n"
        "A candid photo of a robot making coffee.\n\n"
        "🤖 Prompt cho ChatGPT:\n"
        "A candid photo of a robot brewing coffee.\n\n"
        "📝 Gợi ý:\n"
        "- Tỷ lệ: 4:5\n"
        "- Phong cách: đời thường"
    )
    formatted = format_telegram_html(raw)

    assert formatted.count("<pre>") == 2
    assert formatted.count("</pre>") == 2
    assert "<pre>A candid photo of a robot making coffee.</pre>" in formatted
    assert "<pre>A candid photo of a robot brewing coffee.</pre>" in formatted
    # Phần Gợi ý không nằm trong <pre>
    assert "<pre>- Tỷ lệ" not in formatted
    assert "<b>📝 Gợi ý:</b>" in formatted


def test_format_telegram_html_includes_image_analysis_section():
    raw = (
        "🖼️ Phân tích ảnh:\n"
        "- Chủ thể: robot\n\n"
        "🎨 Prompt cho Gemini:\n"
        "robot prompt\n\n"
        "🤖 Prompt cho ChatGPT:\n"
        "robot prompt 2\n\n"
        "📝 Gợi ý:\n"
        "- Tỷ lệ: 1:1"
    )
    formatted = format_telegram_html(raw)
    assert "<b>🖼️ Phân tích ảnh:</b>" in formatted
    assert "<pre>robot prompt</pre>" in formatted
    assert "<pre>robot prompt 2</pre>" in formatted


def test_format_telegram_html_escapes_html_chars():
    raw = "🎨 Prompt cho Gemini:\nA <script>alert(1)</script> & robot\n\n🤖 Prompt cho ChatGPT:\nok"
    formatted = format_telegram_html(raw)
    assert "<script>" not in formatted
    assert "&lt;script&gt;" in formatted


def test_format_telegram_html_fallback_no_headers():
    formatted = format_telegram_html("Just a plain unstructured reply.")
    assert formatted == "<pre>Just a plain unstructured reply.</pre>"


def test_format_telegram_html_empty():
    assert format_telegram_html("") == ""
    assert format_telegram_html(None) == ""
