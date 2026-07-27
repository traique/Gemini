"""Nhánh api1/api2 của provider-chain: gọi Google AI Studio bằng SDK chính
thức (google-genai), 1 client / API key. Độc lập hoàn toàn với nhánh cookie
(ai/cookie_client.py) - không phụ thuộc gemini-webapi hay ChatSession.
"""
import asyncio
import json
import logging
import mimetypes
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from core import config

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_VN_WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

_official_clients: dict[int, object] = {}  # {1: genai.Client, 2: genai.Client}

# generation_config cho nhánh API (persona cần tự nhiên, không máy móc) - áp
# dụng cho chat có persona; ask()/analyze_image() (tác vụ 1 lượt, cần bám sát
# dữ liệu/định dạng) giữ mặc định của SDK để không ảnh hưởng độ chính xác.
_PERSONA_TEMPERATURE = 0.95
_PERSONA_TOP_P = 0.95

_EMBEDDING_MODEL = "text-embedding-004"  # 768 chiều mặc định, khớp cột vector(768) (xem core/database.py)


def api_key_for(idx: int) -> Optional[str]:
    return config.GOOGLE_AI_STUDIO_API_KEY_1 if idx == 1 else config.GOOGLE_AI_STUDIO_API_KEY_2


def _get_official_client(idx: int):
    """Lazy import + khởi tạo client Google AI Studio chính thức cho api{idx}.
    Chỉ được gọi khi key tương ứng đã cấu hình."""
    if idx not in _official_clients:
        from google import genai  # import trễ, tránh lỗi nếu chưa cài package

        key = api_key_for(idx)
        if not key:
            raise RuntimeError(f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}")
        _official_clients[idx] = genai.Client(api_key=key)
    return _official_clients[idx]


def now_vn_context() -> str:
    now = datetime.now(_VN_TZ)
    weekday = _VN_WEEKDAYS[now.weekday()]
    return f"[Thời điểm hiện tại: {now:%H:%M} ngày {now:%d/%m/%Y} ({weekday}), giờ Việt Nam]"


class FallbackResponse:
    """Giả lập interface của ModelOutput (gemini-webapi) cho response từ
    Google AI Studio API chính thức, để handlers/ dùng .text như bình
    thường mà không cần biết câu trả lời đến từ đâu.

    grounding_sources: [(title, uri), ...] lấy từ
    response.candidates[0].grounding_metadata.grounding_chunks khi
    enable_search=True - đây là link THẬT Google Search trả về, khác với
    link mà model tự viết ra trong .text (dễ bịa). Rỗng nếu không bật
    search hoặc response không có grounding (vd model không cần tra cứu)."""

    def __init__(self, text: str, grounding_sources: Optional[list[tuple[str, str]]] = None) -> None:
        self.text = text
        self.used_fallback = True
        self.grounding_sources = grounding_sources or []


def is_quota_exhausted_error(exc: BaseException) -> bool:
    """Bắt ĐÚNG lỗi hết quota (429 / RESOURCE_EXHAUSTED) của google-genai,
    KHÔNG được nhầm với lỗi mạng/timeout thông thường."""
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True
    name = type(exc).__name__
    if "ResourceExhausted" in name:
        return True
    msg = str(exc)
    if "RESOURCE_EXHAUSTED" in msg:
        return True
    if "429" in msg and ("quota" in msg.lower() or "rate" in msg.lower()):
        return True
    return False


def _extract_grounding_sources(response) -> list[tuple[str, str]]:
    """Trích [(title, uri), ...] từ grounding_metadata.grounding_chunks của
    response genai (chỉ có khi enable_search=True và model thực sự có tra
    cứu). Bọc try/except vì đây là cấu trúc SDK có thể đổi/thiếu tuỳ model -
    lỗi ở đây KHÔNG được làm hỏng cả response chính."""
    sources: list[tuple[str, str]] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return sources
        metadata = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(metadata, "grounding_chunks", None) or []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if not web:
                continue
            title = getattr(web, "title", "") or ""
            uri = getattr(web, "uri", "") or ""
            if uri:
                sources.append((title, uri))
    except Exception:
        logger.warning("Không trích được grounding_sources từ response.", exc_info=True)
    return sources


async def generate(
    idx: int,
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    persona_generation_config: bool = False,
    enable_search: bool = False,
    model: Optional[str] = None,
) -> FallbackResponse:
    """Gọi Google AI Studio API (api{idx}) - dùng chung cho ask()/chat() ở
    ai/orchestrator.py.

    history: list [(role, content), ...] cũ -> mới, role là 'user'|'model' -
    nạp làm các lượt hội thoại trước đó (cửa sổ trượt K lượt + session, xem
    core.database.get_session_messages()). Không truyền -> gọi 1 lượt độc lập.

    enable_search: bật Google Search grounding (types.Tool(google_search=...))
    - BẮT BUỘC cho chat() vì chat_skill.yaml yêu cầu Gemini "chủ động tìm
    kiếm thông tin mới nhất"; nếu không có tool thật, model KHÔNG hề tìm
    kiếm được gì mà vẫn tưởng mình "đã tra cứu" -> tự bịa số liệu trình bày
    như thật (nguyên nhân các câu trả lời từng ghi số liệu "ví dụ" bịa đặt).

    model: model ưu tiên của người dùng (lệnh /model, xem
    ai.cookie_client.get_preferred_model_name()). Không truyền hoặc rỗng ->
    dùng config.GOOGLE_AI_STUDIO_MODEL mặc định.
    """
    from google.genai import types  # import trễ, cùng lý do như trên

    client = _get_official_client(idx)
    prompt_with_time = f"{now_vn_context()}\n{prompt}"

    if history:
        contents = []
        for role, content in history:
            genai_role = "model" if role == "model" else "user"
            contents.append(types.Content(role=genai_role, parts=[types.Part.from_text(text=content)]))
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt_with_time)]))
    else:
        contents = prompt_with_time

    cfg_kwargs = {"system_instruction": system_instruction or None}
    if persona_generation_config:
        cfg_kwargs["temperature"] = _PERSONA_TEMPERATURE
        cfg_kwargs["top_p"] = _PERSONA_TOP_P
    if enable_search:
        cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    response = await client.aio.models.generate_content(
        model=model or config.GOOGLE_AI_STUDIO_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    grounding_sources = _extract_grounding_sources(response) if enable_search else []
    return FallbackResponse((response.text or "").strip(), grounding_sources=grounding_sources)


async def generate_image_prompt(idx: int, instruction: str, image_path: str) -> FallbackResponse:
    """Gọi Google AI Studio API (api{idx}) có ảnh, cho tính năng photo_msg
    (ảnh -> viết prompt). Model nhận ảnh làm input và trả text (không tự vẽ
    ảnh, nhưng ở đây ta chỉ cần input ảnh + output text nên vẫn dùng bình thường)."""
    from google.genai import types  # import trễ, cùng lý do như generate()

    client = _get_official_client(idx)

    def _read_bytes() -> bytes:
        with open(image_path, "rb") as f:
            return f.read()

    image_bytes = await asyncio.to_thread(_read_bytes)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"

    response = await client.aio.models.generate_content(
        model=config.GOOGLE_AI_STUDIO_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            instruction,
        ],
    )
    return FallbackResponse((response.text or "").strip())


async def generate_utility_json(prompt: str) -> Optional[dict]:
    """Gọi THẲNG Google AI Studio API (api1, fallback api2) - KHÔNG đi qua
    provider-chain/cookie - cho các tác vụ nội bộ cần output JSON có cấu trúc
    (hiện dùng bởi services/memory_service.py để trích xuất trí nhớ dài hạn
    sau mỗi lượt chat, và services/tools.py cho function-calling router).

    Lý do không dùng cookie cho việc này: gemini-webapi là thư viện
    reverse-engineered giả lập giao diện web, không đảm bảo hỗ trợ
    response_mime_type=json của SDK chính thức. Tác vụ này không phải chat
    trực tiếp với người dùng nên chấp nhận được việc chỉ chạy khi có API key
    chính thức - nếu chưa cấu hình key nào, trả về None và tính năng gọi hàm
    này (trí nhớ dài hạn, function-calling) tự tắt, KHÔNG ảnh hưởng tới
    chat() chính."""
    if not config.HAS_ANY_AI_STUDIO_KEY:
        return None

    from google.genai import types

    last_exc: Optional[BaseException] = None
    for idx in (1, 2):
        if not api_key_for(idx):
            continue
        try:
            client = _get_official_client(idx)
            response = await client.aio.models.generate_content(
                model=config.GOOGLE_AI_STUDIO_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            raw = (response.text or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw)
        except Exception as exc:  # tác vụ nền qua nhiều key, không được raise giữa chừng
            last_exc = exc
            logger.warning(
                "generate_utility_json: lỗi với api%s, thử key kế tiếp nếu có.",
                idx,
                exc_info=True,
            )
            continue

    if last_exc is not None:
        logger.warning("generate_utility_json: hết key khả dụng, bỏ qua tác vụ.")
    return None


class SearchJsonResult:
    """Kết quả của generate_search_json(): JSON có cấu trúc (nếu parse được)
    + grounding_sources thật (link Google Search trả về) + raw_text để debug/
    fallback hiển thị thô khi parse JSON thất bại."""

    def __init__(
        self,
        data: Optional[dict],
        grounding_sources: list[tuple[str, str]],
        raw_text: str,
        used_api_idx: Optional[int],
    ) -> None:
        self.data = data
        self.grounding_sources = grounding_sources
        self.raw_text = raw_text
        self.used_api_idx = used_api_idx


def _parse_json_best_effort(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def generate_search_json(idx: int, prompt: str) -> SearchJsonResult:
    """Gọi THẲNG api{idx} (KHÔNG đi qua provider-chain/cookie) với Google
    Search BẬT + yêu cầu output JSON, cho services/price_service.py (Giai
    đoạn 2 - pipeline giá có kiểm soát). Chỉ gọi 1 key duy nhất mỗi lần -
    việc chọn idx nào, có cooldown hay không, và fallback sang cookie khi
    nào là trách nhiệm của price_service (dispatcher riêng, xem
    services/price_service.py::_fetch_price_data), KHÔNG lặp ở đây, để
    price_service kiểm soát được cooldown/quota nhất quán với provider_state
    dùng chung cho toàn bộ bot.

    ⚠️ google-genai không cho dùng đồng thời response_mime_type="application/json"
    với Google Search tool ở một số model -> thử response_mime_type trước, nếu
    lỗi thì retry KHÔNG có response_mime_type (chỉ dựa vào yêu cầu JSON trong
    prompt), rồi parse best-effort (strip code fence, giống generate_utility_json).

    Raise lại exception cuối nếu cả 2 lần thử đều lỗi (kể cả lỗi quota) - để
    caller (price_service) tự quyết định có nên coi là quota-exhausted và
    chuyển key/provider kế tiếp hay không, giống cách orchestrator xử lý."""
    from google.genai import types

    client = _get_official_client(idx)
    search_tool = [types.Tool(google_search=types.GoogleSearch())]
    last_exc: Optional[BaseException] = None

    for use_json_mime in (True, False):
        cfg_kwargs = {"temperature": 0.1, "tools": search_tool}
        if use_json_mime:
            cfg_kwargs["response_mime_type"] = "application/json"
        try:
            response = await client.aio.models.generate_content(
                model=config.GOOGLE_AI_STUDIO_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            raw_text = (response.text or "").strip()
            data = _parse_json_best_effort(raw_text)
            grounding_sources = _extract_grounding_sources(response)
            return SearchJsonResult(data, grounding_sources, raw_text, used_api_idx=idx)
        except Exception as exc:
            last_exc = exc
            if use_json_mime:
                # Có thể do xung đột response_mime_type + Google Search tool ở
                # model này - thử lại KHÔNG có response_mime_type trước khi
                # coi là lỗi thật (quota/mạng...) và để price_service xử lý.
                logger.info(
                    "generate_search_json: api%s lỗi khi bật response_mime_type "
                    "cùng Google Search, thử lại không ép JSON mime.",
                    idx, exc_info=True,
                )
                continue
            raise

    assert last_exc is not None
    raise last_exc


async def embed_text(text: str) -> Optional[list[float]]:
    """Tạo embedding cho semantic recall (pgvector). LUÔN gọi thẳng Google AI
    Studio API (api1, fallback api2), cùng lý do như generate_utility_json():
    gemini-webapi (cookie) không có API embedding tương đương. Trả None nếu
    chưa cấu hình key hoặc lỗi bất kỳ - tính năng semantic recall tự tắt êm,
    không ảnh hưởng chat chính hay trí nhớ dài hạn dạng facts/summary (vẫn
    hoạt động độc lập với embedding)."""
    if not config.HAS_ANY_AI_STUDIO_KEY:
        return None

    for idx in (1, 2):
        if not api_key_for(idx):
            continue
        try:
            client = _get_official_client(idx)
            result = await client.aio.models.embed_content(
                model=_EMBEDDING_MODEL,
                contents=text,
            )
            embeddings = getattr(result, "embeddings", None)
            if not embeddings:
                return None
            values = getattr(embeddings[0], "values", None)
            return list(values) if values else None
        except Exception:
            logger.warning("embed_text: lỗi với api%s, thử key kế tiếp nếu có.", idx, exc_info=True)
            continue
    return None


async def check_ai_studio_status(idx: int) -> tuple[bool, str]:
    """Gọi thật 1 request rất nhẹ (giới hạn output tối thiểu) để biết
    api{idx} có thực sự dùng được không, thay vì chỉ kiểm tra biến môi
    trường có được set hay không."""
    key = api_key_for(idx)
    if not key:
        return False, f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}"
    try:
        from google.genai import types
    except ImportError:
        return False, "Chưa cài package google-genai (thêm vào requirements.txt rồi redeploy)"
    try:
        client = _get_official_client(idx)
        await client.aio.models.generate_content(
            model=config.GOOGLE_AI_STUDIO_MODEL,
            contents="ping",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
        return True, "OK"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
