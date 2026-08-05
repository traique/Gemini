"""Test tầng trình bày báo cáo phân tích cổ phiếu.

Mỗi test dưới đây tương ứng MỘT lỗi đã thật sự xảy ra trên báo cáo gửi cho
người dùng ngày 05/08/2026 (các mã GEX, FPT, CII). Đây là code liên quan tới
tiền thật nên lỗi trình bày cũng được canh bằng test, không canh bằng mắt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import report_format as rf
from stock import sector as stock_sector


def test_numbers_use_vietnamese_decimal_comma():
    """Báo cáo FPT trộn "-4,76%" với "60.5" và "43.7" trong cùng tin nhắn."""
    assert rf.fmt_price(70370) == "70.370"
    assert rf.fmt_number(-4.76) == "-4,76"
    assert rf.fmt_number(1234.56) == "1.234,56"
    assert rf.fmt_number(60.5, 1) == "60,5"
    assert rf.fmt_signed_pct(3.5, 1) == "+3,5%"
    assert rf.fmt_signed_pct(-1.6, 1) == "-1,6%"
    assert rf.fmt_number(None) == "N/A"


def test_level_always_carries_distance_pct():
    """Báo cáo FPT nêu hỗ trợ 70.370 và kháng cự 74.000 mà không hề nói giá
    hiện tại là bao nhiêu, cũng không nói hai mốc đó cách giá bao xa."""
    assert rf.level_distance_pct(71500, 74000) == 3.5
    assert rf.level_distance_pct(71500, 70370) == -1.58
    text = rf.format_level(71500, 74000, 1)
    assert "74.000" in text
    assert "+3,5%" in text
    assert "1 lần test" in text


def test_far_levels_are_flagged_as_unusable():
    """GEX: kháng cự gần nhất cách +28%, hỗ trợ cách -15%, nhưng báo cáo trình
    bày như vùng mua/bán khả thi."""
    line = rf.nearest_levels_line(24850, [(19700, 1)], [(31900, 2)])
    assert "KHÔNG dùng làm điểm vào/ra ngắn hạn" in line


def test_level_inside_atr_noise_is_flagged():
    """FPT: hỗ trợ 70.370 chỉ cách giá 1,58% trong khi ATR là 2,92% - nhiễu
    một phiên bình thường đủ để xuyên mốc này."""
    line = rf.nearest_levels_line(71500, [(70370, 3)], [(74000, 1)], atr_pct=2.92)
    assert "biên nhiễu một phiên" in line


def test_near_level_without_atr_conflict_has_no_warning():
    line = rf.nearest_levels_line(71500, [(70370, 3)], [(74000, 1)], atr_pct=0.5)
    assert "biên nhiễu một phiên" not in line
    assert "KHÔNG dùng làm điểm vào/ra ngắn hạn" not in line


def test_macd_is_expressed_as_pct_of_price():
    """CII: MACD +24.44 trên cổ phiếu 14.000đ = 0,17% giá, nhưng được diễn giải
    thành "tín hiệu cắt lên" đáng kể."""
    line = rf.macd_strength_line(30.0, 24.44, 14000)
    assert "0,17%" in line
    assert "rất yếu" in line


def test_adx_line_must_state_direction():
    """FPT: ADX 43.7 được gọi là "xu hướng tương đối mạnh" mà không nói mạnh
    theo hướng nào."""
    down = rf.adx_direction_line(43.7, 20.0, 30.0, True)
    assert "nghiêng GIẢM" in down
    up = rf.adx_direction_line(43.7, 30.0, 20.0, True)
    assert "nghiêng TĂNG" in up
    missing = rf.adx_direction_line(0.0, 0.0, 0.0, False, available=False)
    assert "chưa đủ dữ liệu" in missing


def test_news_title_must_mention_the_symbol():
    """FPT và GEX cùng nhận tin "phiên tăng trần" + "phát hành cổ phiếu thưởng"
    - dấu hiệu tin của mã khác bị gán sang."""
    assert rf.title_mentions_symbol("Cổ phiếu FPT tăng trần", "FPT") is True
    assert rf.title_mentions_symbol("cổ phiếu fpt hôm nay", "FPT") is True
    assert rf.title_mentions_symbol("Nhóm VN30 đồng loạt tăng trần", "FPT") is False
    assert rf.title_mentions_symbol("FPTS báo lãi", "FPT") is False


def test_news_impact_ignores_unrelated_headlines():
    """Đây là lỗi nặng nhất của phần tin: sentiment của tin KHÔNG liên quan
    chảy vào news_impact, tức là ảnh hưởng trực tiếp tới khuyến nghị."""
    unrelated = [("Thị trường chung tăng trần", 1.0), ("GELEX lãi lớn", 1.0)]
    assert rf.relevant_news_impact(unrelated, "FPT") == 0.0
    mixed = [("FPT chốt quyền cổ phiếu thưởng", 0.6), ("VN30 giảm sâu", -1.0)]
    assert rf.relevant_news_impact(mixed, "FPT") > 0


def test_duplicate_disclaimer_is_removed():
    """FPT: hai đoạn disclaimer lặp nhau ở cuối; CII cùng prompt lại chỉ có
    một - LLM không ổn định nên phải chặn tất định."""
    text = (
        "Kết luận: HOLD.\n\n"
        "Anh nhớ đây chỉ là tham khảo, không phải khuyến nghị đầu tư tuyệt đối nha.\n\n"
        "Thông tin trên chỉ là tham khảo, không phải khuyến nghị đầu tư ạ."
    )
    cleaned = rf.clean_analysis_output(text)
    assert cleaned.count("tham khảo") == 1
    assert "Kết luận: HOLD." in cleaned


def test_self_intro_and_pet_name_are_removed():
    text = "Anh ơi, em Lan Anh đây ạ! Cập nhật CII cho anh yêu nha."
    cleaned = rf.clean_analysis_output(text)
    assert "Lan Anh đây" not in cleaned
    assert "anh yêu" not in cleaned
    assert "Cập nhật CII" in cleaned


def test_task_done_sentence_is_removed():
    text = "Kết luận: HOLD.\n\nNhiệm vụ phân tích của em xong rồi đó ạ!"
    cleaned = rf.clean_analysis_output(text)
    assert "Nhiệm vụ" not in cleaned


def test_long_paragraph_mentioning_reference_is_kept():
    """Không được xoá đoạn phân tích thật chỉ vì nó có chữ "tham khảo"."""
    long_para = (
        "Vùng 70.370 là mốc tham khảo quan trọng vì đã test 3 lần trong 60 phiên, "
        "và nếu mất mốc này thì kịch bản tăng bị vô hiệu, đồng thời thanh khoản "
        "đang thấp hơn trung bình 20 phiên nên rủi ro trượt giá khi ra hàng là "
        "đáng kể, anh cần tính trước phương án giảm tỷ trọng từng phần thay vì "
        "bán dứt điểm một lần."
    )
    text = f"{long_para}\n\nĐây chỉ là tham khảo, không phải khuyến nghị đầu tư."
    cleaned = rf.clean_analysis_output(text)
    assert "test 3 lần" in cleaned
    assert cleaned.count("không phải khuyến nghị") == 1


def test_cii_and_gex_no_longer_share_one_sector():
    """Lỗi gốc: CII (hạ tầng BOT) và GEX (thiết bị điện) cùng ngành gộp nên hai
    báo cáo khác nhau nhận cùng con số -11,51%/1 tháng."""
    cii = set(stock_sector.get_symbol_sectors("CII"))
    gex = set(stock_sector.get_symbol_sectors("GEX"))
    assert cii and gex
    assert not (cii & gex)
    assert stock_sector.get_primary_sector_label("CII") == "Xây dựng & Hạ tầng"
    assert stock_sector.get_primary_sector_label("GEX") == "Thiết bị điện & Công nghiệp"


def test_industrial_park_symbols_are_separated_from_construction():
    park = set(stock_sector.SECTOR_MAP["industrial_park"]["symbols"])
    construction = set(stock_sector.SECTOR_MAP["construction"]["symbols"])
    assert "KBC" in park
    assert "CII" in construction
    assert not (park & construction)


def test_no_symbol_was_lost_when_splitting_sectors():
    """ALL_KNOWN_SYMBOLS còn là rào nhận diện mã, mất mã ở đây làm việc tra giá
    kém tin cậy đi chứ không chỉ mất phần bình luận ngành."""
    previously_industrial = [
        "GEX", "CTD", "VCG", "REE", "CII", "KBC", "BCM",
        "SIP", "IDC", "HHV", "LCG", "FCN", "TCD",
    ]
    for sym in previously_industrial:
        assert sym in stock_sector.ALL_KNOWN_SYMBOLS, sym
        assert stock_sector.get_symbol_sectors(sym), sym
