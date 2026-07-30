"""Test cho tầng nhận diện mã cổ phiếu trong tin nhắn.

Regression cho bug: mã nằm ngoài ALL_KNOWN_SYMBOLS mà gõ viết thường (vd
"gvr") không được nhận, khiến câu hỏi rơi xuống Gemini không kèm dữ liệu giá
thật và bị trả lời bằng số bịa.
"""
import stock_analysis


def test_lowercase_symbol_with_price_keyword_is_candidate():
    _, unverified = stock_analysis.detect_symbol_candidates("giá gvr")
    assert "GVR" in unverified


def test_lowercase_symbol_with_stock_context_is_candidate():
    _, unverified = stock_analysis.detect_symbol_candidates("cổ phiếu gvr sao rồi")
    assert "GVR" in unverified


def test_uppercase_symbol_still_works_without_any_context():
    _, unverified = stock_analysis.detect_symbol_candidates("GVR")
    assert "GVR" in unverified


def test_bare_lowercase_without_context_is_ignored():
    known, unverified = stock_analysis.detect_symbol_candidates("gvr")
    assert known == []
    assert unverified == []


def test_known_symbol_lowercase_unaffected():
    known, _ = stock_analysis.detect_symbol_candidates("vcb")
    assert "VCB" in known


def test_ambiguous_known_symbol_still_needs_context():
    known, _ = stock_analysis.detect_symbol_candidates("đổ xăng hết bao nhiêu gas")
    assert "GAS" not in known


def test_price_question_without_any_symbol_is_not_guarded():
    assert stock_analysis.looks_like_price_question("giá vàng hôm nay bao nhiêu") is False


def test_price_question_with_unknown_token_is_guarded():
    assert stock_analysis.looks_like_price_question("giá xyz bao nhiêu") is True


def test_casual_message_is_not_guarded():
    assert stock_analysis.looks_like_price_question("tối nay ăn gì anh ơi") is False
