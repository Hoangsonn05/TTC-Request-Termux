# -*- coding: utf-8 -*-
"""TTC API FULL — module độc lập trích từ TTC_FB_Gui_Gemlogin.

Bao gồm toàn bộ lớp API TTC tìm thấy trong mã nguồn được cung cấp:
- Đăng nhập bằng access token và đọc user/số dư.
- Đặt UID Facebook cho tài khoản TTC.
- 7 cặp endpoint lấy job/nhận tiền.
- Chuẩn hóa job, cảm xúc, nội dung bình luận và object ID.
- Gửi submit nhận tiền, phân loại kết quả và lỗi API.

Cài đặt:
    pip install requests
    pip install cloudscraper  # tùy chọn

Ví dụ:
    client = TTCClient()
    account = client.login("YOUR_TTC_TOKEN")
    client.set_uid("FACEBOOK_UID")
    jobs = client.get_jobs(TTCJobType.FOLLOW)
    result = client.submit_job(jobs[0])
"""

import re
import html
from enum import Enum


# =====================================================================
# Bảng endpoint TTC tập trung — không rải URL khắp mã nguồn
# =====================================================================

TTC_BASE_URL = "https://tuongtaccheo.com"

TTC_ENDPOINTS = {
    # job_type_key -> (get_url, claim_url)
    "ttc_like_post_vip_cheo": (
        f"{TTC_BASE_URL}/kiemtien/likepostvipcheo/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/likepostvipcheo/nhantien.php",
    ),
    "ttc_like_post_vip_re": (
        f"{TTC_BASE_URL}/kiemtien/likepostvipre/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/likepostvipre/nhantien.php",
    ),
    "ttc_reaction_vip_cheo": (
        f"{TTC_BASE_URL}/kiemtien/camxucvipcheo/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/camxucvipcheo/nhantien.php",
    ),
    "ttc_reaction_vip_re": (
        f"{TTC_BASE_URL}/kiemtien/camxucvipre/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/camxucvipre/nhantien.php",
    ),
    "ttc_comment": (
        f"{TTC_BASE_URL}/kiemtien/cmtcheo/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/cmtcheo/nhantien.php",
    ),
    "ttc_follow": (
        f"{TTC_BASE_URL}/kiemtien/subcheo/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/subcheo/nhantien.php",
    ),
    "ttc_like_page": (
        f"{TTC_BASE_URL}/kiemtien/likepagecheo/getpost.php",
        f"{TTC_BASE_URL}/kiemtien/likepagecheo/nhantien.php",
    ),
}

TTC_AUTH_URL = f"{TTC_BASE_URL}/logintoken.php"
TTC_SET_UID_URL = f"{TTC_BASE_URL}/cauhinh/datnick.php"

# =====================================================================
# Tất cả 7 loại job được hỗ trợ
# =====================================================================

ALL_JOB_TYPES = list(TTC_ENDPOINTS.keys())

# =====================================================================
# Nhóm action_group
# =====================================================================

ACTION_GROUP_MAP = {
    "ttc_like_post_vip_cheo": "reaction",
    "ttc_like_post_vip_re": "reaction",
    "ttc_reaction_vip_cheo": "reaction",
    "ttc_reaction_vip_re": "reaction",
    "ttc_comment": "comment",
    "ttc_follow": "follow",
    "ttc_like_page": "page_like",
}

# Nhóm loại job theo action_group
ACTION_GROUPS_BLOCKED_BY = {
    # action_group -> list[job_type] bị ảnh hưởng khi group bị block
    "reaction": [
        "ttc_like_post_vip_cheo",
        "ttc_like_post_vip_re",
        "ttc_reaction_vip_cheo",
        "ttc_reaction_vip_re",
    ],
    "comment": ["ttc_comment"],
    "follow": ["ttc_follow"],
    "page_like": ["ttc_like_page"],
}

# =====================================================================
# Reaction type mapping TTC -> nội bộ
# =====================================================================

VALID_REACTIONS = {"LIKE", "LOVE", "CARE", "HAHA", "WOW", "SAD", "ANGRY"}

REACTION_NORMALIZE_MAP = {
    "like": "LIKE",
    "thich": "LIKE",
    "love": "LOVE",
    "yeu thich": "LOVE",
    "care": "CARE",
    "thuong thuong": "CARE",
    "haha": "HAHA",
    "wow": "WOW",
    "sad": "SAD",
    "buon": "SAD",
    "angry": "ANGRY",
    "phan no": "ANGRY",
}


def normalize_reaction_type(raw: str) -> str:
    """Chuẩn hóa reaction type từ dữ liệu TTC về dạng nội bộ.

    TTC trả về loaicx như: LOVE, HAHA, WOW, SAD, ANGRY, CARE.
    Mặc định LIKE nếu không xác định được.
    """
    if not raw:
        return "LIKE"
    upper = str(raw).upper().strip()
    if upper in VALID_REACTIONS:
        return upper
    lower = str(raw).lower().strip()
    return REACTION_NORMALIZE_MAP.get(lower, "LIKE")


# =====================================================================
# Normalize job type
# =====================================================================

def normalize_job_type(job_type: str) -> str:
    """Xác minh job_type hợp lệ, trả về chuỗi gốc nếu OK."""
    if job_type in ALL_JOB_TYPES:
        return job_type
    return ""


def get_action_group(job_type: str) -> str:
    """Lấy action_group từ job_type nội bộ."""
    return ACTION_GROUP_MAP.get(job_type, "unknown")


def get_endpoint_urls(job_type: str):
    """Lấy (get_url, claim_url) theo job_type.

    Raises KeyError nếu job_type không hợp lệ.
    """
    return TTC_ENDPOINTS[job_type]


# =====================================================================
# Tạo job_key xác định, ổn định, không trùng giữa endpoint
# =====================================================================

def make_job_key(job_type: str, idpost: str, reaction_type: str = "") -> str:
    """Tạo job key duy nhất.

    Format: <job_type>:<idpost>[:<reaction_type>]
    Không dùng riêng idpost vì hai endpoint có thể trả cùng id.
    """
    parts = [str(job_type), str(idpost or "")]
    if reaction_type:
        parts.append(str(reaction_type).upper())
    return ":".join(parts)


# =====================================================================
# Parse comment text từ trường nd của TTC
# =====================================================================

def parse_ttc_comment_text(nd: str) -> str:
    """Parser comment text an toàn từ trường nd của TTC.

    Quy tắc:
    1. Nếu nd có nội dung trong dấu nháy kép thì lấy nội dung đó.
    2. Nếu không có dấu nháy nhưng có nội dung hợp lệ thì dùng text đã strip.
    3. Decode HTML entity nếu có.
    4. Không cho comment rỗng.
    5. Giới hạn 500 ký tự.
    6. Không thay đổi nội dung ngoài làm sạch định dạng bao ngoài.
    """
    if not nd:
        return ""
    # Decode HTML entities nếu có
    text = html.unescape(str(nd))

    # Trích nội dung trong dấu nháy kép ngoài cùng
    match = re.search(r'"([^"]+)"', text)
    if match:
        result = match.group(1).strip()
    else:
        result = text.strip()

    # Giới hạn độ dài
    if len(result) > 500:
        result = result[:500]

    return result


# =====================================================================
# Normalize raw TTC job -> schema nội bộ
# =====================================================================

def normalize_object_id(object_id):
    """Chuẩn hóa ID Facebook từ URL hoặc ID thô."""
    from urllib.parse import parse_qs, urlparse

    value = str(object_id or "").strip()
    if not value:
        return ""

    parsed = urlparse(value if "://" in value else "https://facebook.com/" + value.lstrip("/"))
    query = parse_qs(parsed.query)
    for key in ("story_fbid", "fbid", "v", "id"):
        if query.get(key):
            return str(query[key][0]).strip()

    path = parsed.path.strip("/")
    if not path:
        return value

    parts = [part for part in path.split("/") if part]
    for marker in ("posts", "videos", "reel", "reels", "permalink"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return parts[index + 1].split("?")[0].strip()

    numbers = re.findall(r"\d{6,}", path)
    if numbers:
        return numbers[-1]
    return path


_normalize_object_id_impl = normalize_object_id


def normalize_job(job_type: str, raw_data: dict) -> dict:
    """Chuyển raw job TTC về schema nội bộ duy nhất.

    Args:
        job_type: Một trong ALL_JOB_TYPES.
        raw_data: Dict thô từ API TTC.

    Returns:
        Dict schema nội bộ, luôn có đủ các key quan trọng.
        Trả về empty dict nếu thiếu dữ liệu bắt buộc.
    """
    if not isinstance(raw_data, dict):
        return {}

    idpost = str(raw_data.get("idpost") or "").strip()
    link = str(raw_data.get("link") or "").strip()
    action_group = get_action_group(job_type)

    # object_id: ưu tiên normalize từ link, fallback idpost
    object_id = _normalize_object_id_impl(link) if link else ""
    if not object_id:
        object_id = idpost
    if not object_id:
        return {}  # Thiếu dữ liệu bắt buộc

    # Reaction type (chỉ dùng cho reaction group)
    reaction_type = ""
    if action_group == "reaction":
        raw_loai = raw_data.get("loaicx") or ""
        # like_post type không có loaicx → mặc định LIKE
        if job_type in ("ttc_like_post_vip_cheo", "ttc_like_post_vip_re"):
            reaction_type = "LIKE"
        else:
            reaction_type = normalize_reaction_type(raw_loai) if raw_loai else "LIKE"

    # Comment text
    comment_text = ""
    if action_group == "comment":
        comment_text = parse_ttc_comment_text(raw_data.get("nd") or "")

    # job_key
    job_key = make_job_key(job_type, idpost, reaction_type)

    # buff_id dùng làm alias tương thích với JobQueue hiện có
    buff_id = job_key

    return {
        "job_id": job_key,
        "buff_id": buff_id,          # alias tương thích JobQueue
        "type": job_type,
        "action_group": action_group,
        "object_id": object_id,
        "link": link,
        "reaction_type": reaction_type,
        "comment_text": comment_text,
        "ttc_source": job_type,
        "idpost": idpost,            # giữ để gọi nhantien.php
        "raw_data": dict(raw_data),  # debug only, không để GUI phụ thuộc
    }


def normalize_jobs_list(job_type: str, raw_list: list) -> list:
    """Normalize danh sách raw jobs, lọc bỏ job không hợp lệ."""
    result = []
    for raw in raw_list or []:
        job = normalize_job(job_type, raw)
        if job:
            result.append(job)
    return result


def normalize_delay_range(min_delay, max_delay):
    """Chuẩn hóa cặp delay (min, max) về giá trị hợp lệ."""
    try:
        min_delay = int(min_delay)
        max_delay = int(max_delay)
    except (TypeError, ValueError):
        return 5, 15
    if min_delay < 0 or max_delay < 0:
        return 5, 15
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay
    min_delay = min(min_delay, 3600)
    max_delay = min(max_delay, 3600)
    return min_delay, max_delay

from dataclasses import dataclass


@dataclass
class TTCClaimResult:
    success: bool
    reason: str
    message: str
    earned_coin: int = 0
    balance: int | None = None
    http_status: int | None = None
    retryable: bool = False
    endpoint_key: str = ""
    evidence: str = ""
    response_kind: str = ""
    response_keys: tuple[str, ...] = ()
    raw_summary: str = ""

    def to_dict(self):
        return {
            "success": self.success,
            "reason": self.reason,
            "message": self.message,
            "earned_coin": self.earned_coin,
            "balance": self.balance,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "endpoint_key": self.endpoint_key,
            "evidence": self.evidence,
            "response_kind": self.response_kind,
            "response_keys": self.response_keys,
            "raw_summary": self.raw_summary,
        }

import json
import re
import time
import hashlib
import logging
import html
import unicodedata

try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

import requests


logger = logging.getLogger(__name__)

# =====================================================================
# Custom exceptions
# =====================================================================


class TTCError(Exception):
    """Base exception cho táº¥t cáº£ lá»—i TTC."""


class TTCAuthError(TTCError):
    """Token TTC khÃ´ng há»£p lá»‡ hoáº·c Ä‘Ã£ háº¿t háº¡n."""


class TTCNoJobAvailable(TTCError):
    """Endpoint khÃ´ng cÃ³ job nÃ o phÃ¹ há»£p (khÃ´ng pháº£i lá»—i API tháº­t)."""

    def __init__(self, message="", countdown=0):
        super().__init__(message)
        self.ttc_message = message
        self.countdown = int(countdown or 0)


class TTCRateLimitError(TTCError):
    """Server TTC Ä‘ang rate limit request."""


class TTCResponseError(TTCError):
    """Response khÃ´ng Ä‘Ãºng format hoáº·c HTTP lá»—i."""


# =====================================================================
# Helpers ná»™i bá»™
# =====================================================================

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/114.0.0.0 Safari/537.36"
)

_HEADERS_BASE = {
    "User-Agent": _UA,
    "x-requested-with": "XMLHttpRequest",
    "content-type": "application/x-www-form-urlencoded",
}


def _mask_token(token: str) -> str:
    """Che token, chá»‰ log hash ngáº¯n."""
    if not token or len(token) < 8:
        return "***"
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"***[hash:{h}]"


_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)\b(access_token|authorization|cookie|session|token|password|pass|auth)\b\s*[:=]\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&<>]+)"
)
_MOJIBAKE_MARKERS = ("Ãƒ", "Ã‚", "Ã„", "Ã¡Âº", "Ã¡Â»", "chÃƒ", "Ã„â€˜", "Ã†", "Ã")


def _mask_sensitive_text(value: str) -> str:
    text = str(value or "")
    text = _SENSITIVE_FIELD_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = re.sub(r"(?i)(access_token|cookie|session|token)=\S+", r"\1=***", text)
    return text


def _mojibake_score(text: str) -> int:
    value = str(text or "")
    return sum(value.count(marker) for marker in _MOJIBAKE_MARKERS) + value.count("\ufffd") * 2


def _repair_mojibake_if_needed(text: str) -> str:
    current = str(text or "")
    if _mojibake_score(current) <= 0:
        return current
    best = current
    best_score = _mojibake_score(best)
    candidate = current
    for _ in range(2):
        try:
            candidate = candidate.encode("latin-1").decode("utf-8")
        except UnicodeError:
            break
        score = _mojibake_score(candidate)
        if score < best_score:
            best = candidate
            best_score = score
    return best


def _charset_from_content_type(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*([^\s;]+)", str(content_type or ""), flags=re.I)
    return match.group(1).strip("\"'") if match else ""


def _decode_response_body(response):
    """Decode TTC response bytes with UTF-8-first fallback and controlled mojibake repair."""
    headers = getattr(response, "headers", {}) or {}
    content_type = headers.get("content-type", "") or headers.get("Content-Type", "")
    raw = getattr(response, "content", b"") or b""
    if not isinstance(raw, (bytes, bytearray)):
        raw = b""
    if not raw and getattr(response, "text", None) is not None:
        raw = str(response.text or "").encode("utf-8", errors="replace")

    candidates = []
    charset = _charset_from_content_type(content_type)
    response_encoding = str(getattr(response, "encoding", "") or "")
    apparent_encoding = str(getattr(response, "apparent_encoding", "") or "")
    for enc in (charset, "utf-8", response_encoding, apparent_encoding, "latin-1"):
        enc_norm = str(enc or "").strip()
        if not enc_norm:
            continue
        if enc_norm.lower() == "iso-8859-1" and not charset:
            continue
        if enc_norm.lower() not in [item.lower() for item in candidates]:
            candidates.append(enc_norm)

    decoded = ""
    best_score = None
    best_encoding = ""
    for enc in candidates or ["utf-8"]:
        try:
            text = bytes(raw).decode(enc, errors="replace")
        except LookupError:
            continue
        repaired = _repair_mojibake_if_needed(text)
        score = _mojibake_score(repaired)
        if best_score is None or score < best_score:
            decoded = repaired
            best_score = score
            best_encoding = enc
        if score == 0 and enc.lower().replace("_", "-") == "utf-8":
            break

    if not decoded and getattr(response, "text", None) is not None:
        decoded = _repair_mojibake_if_needed(response.text or "")
        best_encoding = "response.text"

    return decoded, {
        "content_type": content_type,
        "encoding": best_encoding,
        "charset": charset,
        "raw_size": len(raw),
        "mojibake_score": _mojibake_score(decoded),
    }


def _safe_json(response, label="response", allow_plain_text=False):
    """Parse JSON from decoded body; optionally return plain text for claim diagnostics."""
    decoded_text, _meta = _decode_response_body(response)
    try:
        return json.loads(decoded_text)
    except Exception as e:
        if allow_plain_text:
            return decoded_text
        snippet = _safe_display_message(decoded_text, limit=200)
        raise TTCResponseError(
            f"[TTC] {label}: Không parse được JSON. "
            f"Status={response.status_code}. Snippet: {snippet!r}"
        ) from e


def _parse_earned_coin(message: str) -> int:
    """Parse sá»‘ xu kiáº¿m Ä‘Æ°á»£c tá»« message TTC.

    Há»— trá»£ dáº¥u cháº¥m vÃ  dáº¥u pháº©y trong sá»‘ (vd: 1.000, 1,000).
    """
    if not message:
        return 0
    # TÃ¬m pattern "cá»™ng <sá»‘> xu" hoáº·c "cá»™ng <sá»‘>xu"
    # Sá»‘ cÃ³ thá»ƒ chá»©a dáº¥u . hoáº·c , ngÄƒn cÃ¡ch hÃ ng nghÃ¬n
    folded = _strip_accents(message).casefold()
    match = re.search(r"cong\s+([\d.,]+)\s*xu", folded, flags=re.IGNORECASE | re.UNICODE)
    if not match:
        match = re.search(r"c\S*ng\s+([\d.,]+)\s*xu", str(message or "").lower(), flags=re.IGNORECASE | re.UNICODE)
    if not match:
        # Thá»­ tÃ¬m sá»‘ ngay sau "cá»™ng "
        match = re.search(r"cong\s+([\d.,]+)", folded, flags=re.IGNORECASE | re.UNICODE)
    if not match:
        match = re.search(r"c\S*ng\s+([\d.,]+)", str(message or "").lower(), flags=re.IGNORECASE | re.UNICODE)
    if not match:
        return 0
    raw_num = match.group(1).replace(",", "").replace(".", "")
    try:
        return int(raw_num)
    except ValueError:
        return 0


def _parse_countdown(data) -> int:
    """Láº¥y giÃ¡ trá»‹ countdown tá»« response dict."""
    if not isinstance(data, dict):
        return 0
    val = data.get("countdown") or data.get("time") or data.get("wait") or 0
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0


def _extract_message(data) -> str:
    """Extract TTC message from common top-level, nested, JSON-string, or text responses."""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, (int, float, bool)):
        return str(data)
    if isinstance(data, list):
        for item in data:
            message = _extract_message(item)
            if message:
                return message
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("mess", "message", "msg", "notice", "status_text", "text"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value).strip()
    nested = data.get("data")
    if isinstance(nested, (dict, list, str)):
        message = _extract_message(nested)
        if message:
            return message
    for value in data.values():
        if isinstance(value, dict):
            message = _extract_message(value)
            if message:
                return message
    return ""


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", str(value or ""))
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D")


def _safe_display_message(message: str, limit: int = 240) -> str:
    text = html.unescape(_repair_mojibake_if_needed(str(message or "")))
    text = re.sub(r"<[^>]+>", " ", text)
    text = _mask_sensitive_text(text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _response_kind_and_keys(data):
    if isinstance(data, dict):
        data_type = "dict"
        keys = tuple(str(k) for k in list(data.keys())[:12])
    elif isinstance(data, list):
        data_type = "list"
        keys = (f"len={len(data)}",)
    elif isinstance(data, str):
        stripped = data.strip()
        data_type = "html" if stripped.startswith("<") and re.search(r"(?i)<\s*(html|!doctype|body)\b", stripped[:200]) else ("empty" if not stripped else "text")
        keys = ()
    else:
        data_type = type(data).__name__
        keys = ()
    return data_type, keys


def _summarize_claim_response(data, response_text="", http_status=None, content_type="", endpoint_key="", idpost="", loaicx="") -> str:
    data_type, keys_tuple = _response_kind_and_keys(data)
    keys = ",".join(keys_tuple)
    snippet = _safe_display_message(response_text, limit=300)
    return (
        f"endpoint={endpoint_key or ''} | idpost={idpost or ''} | loaicx={loaicx or ''} "
        f"| http={http_status} | content_type={content_type or ''} "
        f"| response_kind={data_type} | keys={keys or '-'} | raw_summary={snippet or '-'}"
    )


def _dict_success_flag(data) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("success", "status", "ok"):
        value = data.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().casefold() in ("true", "ok", "success", "1"):
            return True
    nested = data.get("data")
    return _dict_success_flag(nested) if isinstance(nested, dict) else False


def _classify_claim_response(data, message: str, http_status=None, endpoint_key="", evidence="", raw_summary="") -> TTCClaimResult:
    clean_message = _safe_display_message(message)
    folded = _strip_accents(clean_message).casefold()
    response_kind, response_keys = _response_kind_and_keys(data)
    success_flag = _dict_success_flag(data)
    parsed_coin = _parse_earned_coin(clean_message)
    has_success_text = "thanh cong" in folded or re.search(r"cong\s+[\d.,]+\s*xu", folded) or parsed_coin > 0
    has_error_text = any(
        marker in folded
        for marker in (
            "that bai", "loi", "khong thanh cong", "chua", "khong tim thay",
            "het han", "dang nhap", "huy", "sai", "invalid",
        )
    )
    kwargs = {
        "http_status": http_status,
        "endpoint_key": endpoint_key,
        "evidence": evidence,
        "response_kind": response_kind,
        "response_keys": response_keys,
        "raw_summary": _safe_display_message(raw_summary or evidence, limit=300),
    }
    if response_kind == "html":
        return TTCClaimResult(
            False,
            "INVALID_RESPONSE",
            clean_message or "TTC trả về HTML thay vì dữ liệu claim",
            retryable=False,
            **kwargs,
        )
    if (success_flag or has_success_text) and not has_error_text:
        return TTCClaimResult(True, "SUCCESS", clean_message, parsed_coin, **kwargs)
    if any(marker in folded for marker in ("chua lam", "chua tuong tac", "chua xac nhan", "chua thay", "not observed")):
        return TTCClaimResult(False, "NOT_OBSERVED_YET", clean_message, retryable=True, **kwargs)
    if any(marker in folded for marker in ("da nhan", "da duoc nhan", "already")):
        return TTCClaimResult(False, "ALREADY_CLAIMED", clean_message, retryable=False, **kwargs)
    if any(marker in folded for marker in ("dang nhap", "vui long dang nhap", "het han", "login", "unauthorized", "auth")):
        return TTCClaimResult(False, "AUTH_EXPIRED", clean_message, retryable=False, **kwargs)
    if any(marker in folded for marker in ("rate limit", "qua nhanh", "thu lai sau", "cho")):
        return TTCClaimResult(False, "RATE_LIMITED", clean_message, retryable=True, **kwargs)
    if any(marker in folded for marker in ("id", "job", "nhiem vu", "khong ton tai", "huy")):
        return TTCClaimResult(False, "INVALID_JOB", clean_message, retryable=False, **kwargs)
    if not clean_message:
        return TTCClaimResult(
            False,
            "UNKNOWN_RESPONSE",
            "TTC không trả về thông báo nhận diện được",
            retryable=False,
            **kwargs,
        )
    return TTCClaimResult(False, "TTC_REJECTED", clean_message, retryable=False, **kwargs)


# =====================================================================
# TTCClient
# =====================================================================


class TTCClient:
    """HTTP client TTC cho má»™t worker.

    Má»—i worker táº¡o má»™t instance riÃªng â€” khÃ´ng chia sáº» session/cookie.
    """

    def __init__(self, worker_id="?", log_fn=None):
        """
        Args:
            worker_id: ID worker dÃ¹ng Ä‘á»ƒ prefix log.
            log_fn: callable(message, level) hoáº·c None (dÃ¹ng print).
        """
        self.worker_id = str(worker_id)
        self._log_fn = log_fn
        self._session = self._create_session()
        self._token = ""
        self._user = ""
        self._initial_balance = None
        self._authenticated = False

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _create_session(self):
        if _HAS_CLOUDSCRAPER:
            return cloudscraper.create_scraper()
        return requests.Session()

    def _log(self, message: str, level: str = "api"):
        if self._log_fn:
            self._log_fn(message, level)
        else:
            print(f"[TTC Worker #{self.worker_id}] [{level.upper()}] {message}")

    def _headers(self) -> dict:
        return dict(_HEADERS_BASE)

    # ------------------------------------------------------------------
    # authenticate()
    # ------------------------------------------------------------------

    def authenticate(self, token: str) -> None:
        """ÄÄƒng nháº­p TTC báº±ng access_token.

        Args:
            token: TTC token (khÃ´ng log Ä‘áº§y Ä‘á»§).

        Raises:
            TTCAuthError: Token sai hoáº·c response khÃ´ng há»£p lá»‡.
            TTCResponseError: Response khÃ´ng parse Ä‘Æ°á»£c.
            TTCRateLimitError: Rate limit tá»« server.
        """
        self._token = ""
        self._user = ""
        self._initial_balance = None
        self._authenticated = False

        if not token or not str(token).strip():
            raise TTCAuthError("[TTC] Token rỗng, không thể đăng nhập.")

        self._token = str(token).strip()
        masked = _mask_token(self._token)
        self._log(f"Đang đăng nhập TTC với token {masked}...", "auth")

        try:
            resp = self._session.post(
                TTC_AUTH_URL,
                data={"access_token": self._token},
                headers=self._headers(),
                timeout=15,
            )
        except requests.exceptions.Timeout as e:
            raise TTCResponseError(f"[TTC] Timeout khi đăng nhập: {e}") from e
        except requests.exceptions.RequestException as e:
            raise TTCResponseError(f"[TTC] Lỗi mạng khi đăng nhập: {e}") from e

        if resp.status_code == 429:
            raise TTCRateLimitError("[TTC] Server TTC đang rate limit.")
        if resp.status_code >= 500:
            raise TTCResponseError(
                f"[TTC] Server TTC lỗi {resp.status_code} khi đăng nhập."
            )
        if resp.status_code >= 400:
            raise TTCAuthError(
                f"[TTC] HTTP {resp.status_code} khi đăng nhập. Token có thể sai."
            )

        data = _safe_json(resp, "authenticate")

        raw_data = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw_data, dict):
            raise TTCAuthError(
                f"[TTC] Response đăng nhập thiếu trường 'data': {str(data)[:200]}"
            )

        user = raw_data.get("user") or raw_data.get("username") or ""
        if not user:
            raise TTCAuthError(
                f"[TTC] Response đăng nhập thiếu trường 'user'. Response: {str(raw_data)[:200]}"
            )

        if "sodu" not in raw_data or raw_data["sodu"] is None:
            raise TTCResponseError(
                "[TTC] Authentication response is missing required field 'sodu'."
            )

        sodu = raw_data["sodu"]
        if isinstance(sodu, bool):
            raise TTCResponseError(
                "[TTC] Authentication response field 'sodu' is not a valid integer."
            )
        if isinstance(sodu, int):
            balance = sodu
        elif isinstance(sodu, str) and re.fullmatch(r"[+-]?\d+", sodu.strip()):
            balance = int(sodu.strip())
        else:
            raise TTCResponseError(
                "[TTC] Authentication response field 'sodu' is not a valid integer."
            )

        self._user = str(user)
        self._initial_balance = balance
        self._authenticated = True

        self._log(
            f"Đăng nhập TTC thành công. User: {self._user} | Số dư: {balance} xu",
            "auth",
        )

    # ------------------------------------------------------------------
    # configure_facebook_uid()
    # ------------------------------------------------------------------

    def configure_facebook_uid(self, uid: str) -> None:
        """Äáº·t UID Facebook cho tÃ i khoáº£n TTC.

        Chá»‰ gá»i sau khi authenticate() thÃ nh cÃ´ng.

        Raises:
            TTCAuthError: ChÆ°a Ä‘Äƒng nháº­p.
            TTCResponseError: Request lá»—i hoáº·c server khÃ´ng xÃ¡c nháº­n.
        """
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi đặt UID.")
        if not uid or not str(uid).strip():
            raise ValueError("[TTC] UID Facebook rỗng, không thể đặt nick.")

        uid = str(uid).strip()
        self._log(f"Đang đặt UID Facebook TTC: uid={uid}", "auth")

        try:
            resp = self._session.get(
                TTC_SET_UID_URL,
                headers=self._headers(),
                data={
                    "iddat[]": uid,
                    "loai": "fb",
                },
                timeout=15,
            )
        except requests.exceptions.Timeout as e:
            raise TTCResponseError(f"[TTC] Timeout khi đặt UID: {e}") from e
        except requests.exceptions.RequestException as e:
            raise TTCResponseError(f"[TTC] Lỗi mạng khi đặt UID: {e}") from e

        if resp.status_code >= 400:
            raise TTCResponseError(
                f"[TTC] HTTP {resp.status_code} khi đặt UID Facebook."
            )

        # Parse response Ä‘á»ƒ xÃ¡c minh
        try:
            data = resp.json()
        except Exception:
            data = {}

        message = _extract_message(data)
        self._log(f"Đã đặt UID Facebook: uid={uid}. Server: {message or '(ok)'}", "auth")

    # ------------------------------------------------------------------
    # get_account_info()
    # ------------------------------------------------------------------

    def get_account_info(self) -> dict:
        """Láº¥y thÃ´ng tin user vÃ  sá»‘ dÆ° tá»« session hiá»‡n táº¡i."""
        return {
            "user": self._user,
            "balance": self._initial_balance,
            "authenticated": self._authenticated,
        }

    # ------------------------------------------------------------------
    # fetch_jobs()
    # ------------------------------------------------------------------

    def fetch_jobs(self, job_type: str) -> list:
        """Láº¥y danh sÃ¡ch job tá»« endpoint TTC.

        Args:
            job_type: Má»™t trong ALL_JOB_TYPES.

        Returns:
            List cÃ¡c job Ä‘Ã£ normalize, sáºµn sÃ ng Ä‘Æ°a vÃ o JobQueue.

        Raises:
            TTCNoJobAvailable: KhÃ´ng cÃ³ job (khÃ´ng pháº£i lá»—i API).
            TTCAuthError: PhiÃªn Ä‘Ã£ háº¿t háº¡n.
            TTCRateLimitError: Rate limit.
            TTCResponseError: Lá»—i HTTP hoáº·c format.
            ValueError: job_type khÃ´ng há»£p lá»‡.
        """
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi fetch_jobs.")
        if job_type not in TTC_ENDPOINTS:
            raise ValueError(f"[TTC] job_type khÃ´ng há»£p lá»‡: {job_type!r}")

        get_url, _ = TTC_ENDPOINTS[job_type]

        try:
            resp = self._session.get(
                get_url,
                headers=self._headers(),
                timeout=15,
            )
        except requests.exceptions.Timeout as e:
            raise TTCResponseError(
                f"[TTC] Timeout khi láº¥y job {job_type}: {e}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise TTCResponseError(
                f"[TTC] Lá»—i máº¡ng khi láº¥y job {job_type}: {e}"
            ) from e

        if resp.status_code == 429:
            raise TTCRateLimitError(f"[TTC] Rate limit khi láº¥y job {job_type}.")
        if resp.status_code == 401 or resp.status_code == 403:
            raise TTCAuthError(
                f"[TTC] HTTP {resp.status_code}: Phiên đăng nhập có thể đã hết hạn."
            )
        if resp.status_code >= 500:
            raise TTCResponseError(
                f"[TTC] Server lá»—i {resp.status_code} khi láº¥y job {job_type}."
            )
        if resp.status_code >= 400:
            raise TTCResponseError(
                f"[TTC] HTTP {resp.status_code} khi láº¥y job {job_type}."
            )

        # Parse fetch responses using the endpoint's fail-closed contract.
        raw_text = (resp.text or "").strip()
        if not raw_text:
            raise TTCResponseError(f"[TTC] Empty response from {job_type}.")

        try:
            data = resp.json()
        except Exception:
            # KhÃ´ng pháº£i JSON â€” cÃ³ thá»ƒ lÃ  HTML lá»—i hoáº·c empty
            snippet = raw_text[:200]
            raise TTCResponseError(
                f"[TTC] Response khÃ´ng pháº£i JSON tá»« {job_type}. Snippet: {snippet!r}"
            )

        # Case 1: List cÃ³ job
        if isinstance(data, list) and len(data) > 0:
            jobs = normalize_jobs_list(job_type, data)
            return jobs

        # Case 2: List rá»—ng
        if isinstance(data, list) and len(data) == 0:
            raise TTCNoJobAvailable(f"[TTC] KhÃ´ng cÃ³ job {job_type}.", 0)

        # Case 3: Dict responses fail closed. Error semantics take precedence;
        # countdown alone is not evidence that TTC has no jobs.
        if isinstance(data, dict):
            message = _extract_message(data)
            error_value = data.get("error")
            has_explicit_error = (
                "error" in data and error_value not in (None, "", False)
            )
            if isinstance(error_value, (str, int, float, bool)):
                error_message = str(error_value)
            else:
                error_message = _extract_message(error_value)

            semantic_text = " ".join(
                part for part in (message, error_message) if part
            )
            folded = _strip_accents(semantic_text).casefold()
            display_message = _safe_display_message(semantic_text)

            if any(
                marker in folded
                for marker in ("dang nhap", "login", "expired", "unauthorized")
            ):
                raise TTCAuthError(
                    f"[TTC] Authentication failure while fetching {job_type}: "
                    f"{display_message or 'authentication error'}"
                )

            if (
                re.search(r"\brate[\s_-]*limit(?:ed|ing)?\b", folded)
                or "too many request" in folded
            ):
                raise TTCRateLimitError(
                    f"[TTC] Rate limit while fetching {job_type}: "
                    f"{display_message or 'rate limited'}"
                )

            if has_explicit_error:
                raise TTCResponseError(
                    f"[TTC] API error while fetching {job_type}: "
                    f"{_safe_display_message(error_message or message) or 'explicit error'}"
                )

            _response_kind, response_keys = _response_kind_and_keys(data)
            raise TTCResponseError(
                f"[TTC] Unknown response dict from {job_type}. "
                f"Keys={','.join(response_keys) or '-'}; "
                f"Message={_safe_display_message(message) or '-'}"
            )

        raise TTCResponseError(
            f"[TTC] Response format khÃ´ng xÃ¡c Ä‘á»‹nh tá»« {job_type}: {type(data).__name__}"
        )

    # ------------------------------------------------------------------
    # claim_reward()
    # ------------------------------------------------------------------

    def claim_reward(self, job: dict) -> dict:
        """Nháº­n xu sau khi thao tÃ¡c Facebook thÃ nh cÃ´ng.

        Chá»‰ gá»i hÃ m nÃ y sau khi xÃ¡c nháº­n Facebook action = True.

        Args:
            job: Dict job ná»™i bá»™ Ä‘Ã£ normalize.

        Returns:
            {
                "success": bool,
                "message": str,
                "earned_coin": int,
                "balance": int | None,
            }

        Raises:
            TTCAuthError: PhiÃªn háº¿t háº¡n.
            TTCResponseError: Lá»—i máº¡ng/HTTP.
        """
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi claim_reward.")

        job_type = job.get("type", "")
        if job_type not in TTC_ENDPOINTS:
            raise ValueError(f"[TTC] job_type khÃ´ng há»£p lá»‡ khi claim: {job_type!r}")

        _, claim_url = TTC_ENDPOINTS[job_type]
        idpost = str(job.get("idpost") or "").strip()
        if not idpost:
            raise ValueError("[TTC] Thiáº¿u idpost khi claim_reward.")

        # Payload theo loáº¡i job
        action_group = job.get("action_group", "")
        payload = {"id": idpost}
        if action_group == "reaction":
            reaction_type = job.get("reaction_type", "")
            # like_post chá»‰ cáº§n id, reaction group cáº§n loaicx
            if job_type in ("ttc_reaction_vip_cheo", "ttc_reaction_vip_re"):
                payload["loaicx"] = reaction_type

        try:
            resp = self._session.post(
                claim_url,
                headers=self._headers(),
                data=payload,
                timeout=15,
            )
        except requests.exceptions.Timeout as e:
            raise TTCResponseError(
                f"[TTC] Timeout khi claim reward job_type={job_type}: {e}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise TTCResponseError(
                f"[TTC] Lá»—i máº¡ng khi claim reward job_type={job_type}: {e}"
            ) from e

        if resp.status_code == 401 or resp.status_code == 403:
            raise TTCAuthError(
                f"[TTC] HTTP {resp.status_code}: Phiên đăng nhập hết hạn khi claim."
            )
        if resp.status_code >= 400:
            raise TTCResponseError(
                f"[TTC] HTTP {resp.status_code} khi claim reward job_type={job_type}."
            )

        decoded_text, decode_meta = _decode_response_body(resp)
        data = _safe_json(resp, f"claim_reward({job_type})", allow_plain_text=True)
        message = _extract_message(data)
        evidence = _summarize_claim_response(
            data,
            response_text=decoded_text,
            http_status=resp.status_code,
            content_type=decode_meta.get("content_type", "") or (resp.headers.get("content-type", "") if getattr(resp, "headers", None) else ""),
            endpoint_key=job_type,
            idpost=idpost,
            loaicx=payload.get("loaicx", ""),
        )
        claim = _classify_claim_response(
            data,
            message,
            http_status=resp.status_code,
            endpoint_key=job_type,
            evidence=evidence,
            raw_summary=_safe_display_message(decoded_text, limit=300),
        )

        # Äá»c sá»‘ dÆ° náº¿u cÃ³
        balance = None
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            raw_balance = data["data"].get("sodu")
            if raw_balance is not None:
                try:
                    balance = int(raw_balance)
                except (TypeError, ValueError):
                    pass

        claim.balance = balance
        return claim.to_dict()

    # ------------------------------------------------------------------
    # close()
    # ------------------------------------------------------------------

    def close(self) -> None:
        """ÄÃ³ng HTTP session."""
        try:
            self._session.close()
        except Exception:
            pass
        self._authenticated = False

# =====================================================================
# Tên gọi thuận tiện khi tích hợp sang tool khác
# =====================================================================

class TTCJobType(str, Enum):
    LIKE_POST_VIP_CHEO = "ttc_like_post_vip_cheo"
    LIKE_POST_VIP_RE = "ttc_like_post_vip_re"
    REACTION_VIP_CHEO = "ttc_reaction_vip_cheo"
    REACTION_VIP_RE = "ttc_reaction_vip_re"
    COMMENT = "ttc_comment"
    FOLLOW = "ttc_follow"
    LIKE_PAGE = "ttc_like_page"


def _login_alias(self, token: str):
    self.authenticate(token)
    return self.get_account_info()


def _set_uid_alias(self, uid: str):
    return self.configure_facebook_uid(uid)


def _get_jobs_alias(self, job_type):
    key = job_type.value if isinstance(job_type, TTCJobType) else str(job_type)
    return self.fetch_jobs(key)


def _submit_job_alias(self, job: dict):
    return self.claim_reward(job)


TTCClient.login = _login_alias
TTCClient.set_uid = _set_uid_alias
TTCClient.get_jobs = _get_jobs_alias
TTCClient.submit_job = _submit_job_alias


__all__ = [
    "TTC_BASE_URL",
    "TTC_AUTH_URL",
    "TTC_SET_UID_URL",
    "TTC_ENDPOINTS",
    "ALL_JOB_TYPES",
    "ACTION_GROUP_MAP",
    "ACTION_GROUPS_BLOCKED_BY",
    "VALID_REACTIONS",
    "TTCJobType",
    "TTCClient",
    "TTCClaimResult",
    "TTCError",
    "TTCAuthError",
    "TTCNoJobAvailable",
    "TTCRateLimitError",
    "TTCResponseError",
    "normalize_reaction_type",
    "normalize_job_type",
    "get_action_group",
    "get_endpoint_urls",
    "normalize_object_id",
    "normalize_job",
    "normalize_jobs_list",
    "parse_ttc_comment_text",
]
