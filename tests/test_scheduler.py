"""Unit test cho scheduler.py - đặc biệt thứ tự gửi/mark_reminder_sent (#7):
reminder chỉ được đánh dấu đã gửi SAU KHI gửi Telegram thành công, để lỗi gửi
tạm thời không làm mất reminder vĩnh viễn."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database as db  # noqa: E402
import scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_notify_callback():
    yield
    scheduler._notify_callback = None


@pytest.mark.asyncio
async def test_notify_tra_true_khi_gui_thanh_cong(monkeypatch):
    async def fake_callback(uid, text):
        return None

    scheduler.set_notify_callback(fake_callback)
    assert await scheduler._notify(1, "xin chao") is True


@pytest.mark.asyncio
async def test_notify_tra_false_khi_gui_loi(monkeypatch):
    async def fake_callback(uid, text):
        raise RuntimeError("mô phỏng lỗi Telegram")

    scheduler.set_notify_callback(fake_callback)
    assert await scheduler._notify(1, "xin chao") is False


@pytest.mark.asyncio
async def test_notify_tra_false_khi_chua_dang_ky_callback():
    assert await scheduler._notify(1, "xin chao") is False


@pytest.mark.asyncio
async def test_reminder_gui_that_bai_khong_bi_mark_sent(monkeypatch):
    mark_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def failing_callback(uid, text):
        raise RuntimeError("Telegram tạm thời lỗi")

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    scheduler.set_notify_callback(failing_callback)

    await scheduler._process_due_reminders([(101, 1, "uống thuốc")])

    assert mark_calls == []  # gửi lỗi -> KHÔNG được mark sent, để lượt sau thử lại


@pytest.mark.asyncio
async def test_reminder_gui_thanh_cong_moi_mark_sent(monkeypatch):
    mark_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def ok_callback(uid, text):
        return None

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    scheduler.set_notify_callback(ok_callback)

    await scheduler._process_due_reminders([(102, 1, "họp lúc 3h")])

    assert mark_calls == [102]


@pytest.mark.asyncio
async def test_reminder_nhieu_reminder_doc_lap_nhau(monkeypatch):
    """1 reminder gửi lỗi không được chặn các reminder khác trong cùng lượt quét."""
    mark_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def flaky_callback(uid, text):
        if "loi" in text:
            raise RuntimeError("mô phỏng lỗi")

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    scheduler.set_notify_callback(flaky_callback)

    await scheduler._process_due_reminders([
        (1, 1, "binh thuong 1"),
        (2, 1, "loi"),
        (3, 1, "binh thuong 2"),
    ])

    assert mark_calls == [1, 3]
