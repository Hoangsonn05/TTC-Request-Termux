from __future__ import annotations

import sys

MINIMUM_PYTHON = (3, 9)

if sys.version_info < MINIMUM_PYTHON:
    raise SystemExit("api_ttc.py requires Python 3.9 or newer.")

import requests
import time
import hashlib
import json
import os
import base64
import binascii
from colorama import Fore, Style, init
import uuid
import pyotp
import random
import getpass
import contextlib
import re
import unicodedata
import math
import html
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import unquote

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

init(autoreset=True)

total_coin = 0
SUPPORTED_JOB_TYPES = frozenset({'like_facebook', 'like_poster'})
TTC_DIRECT_POST_ID_JOB_TYPES = frozenset({
    "ttc_like_post_vip_re",
    "ttc_reaction_vip_re",
})
FB_READ_TIMEOUT = (10, 40)
FB_ACTION_TIMEOUT = (10, 45)
FB_LOGIN_TIMEOUT = (10, 45)
NETWORK_RETRY_DELAY = 2
TTC_CLAIM_RETRY_DELAY = 2
TTC_ACQUISITION_POLL_SECONDS = 10
FACEBOOK_REACTION_TRANSIENT_SUMMARY = "something went wrong"
FACEBOOK_REACTION_TRANSIENT_DESCRIPTION = (
    "we're having trouble completing your request. please try again."
)
INTERACTION_BLOCKED = "INTERACTION_BLOCKED"
INTERACTION_BLOCKED_EVENT = "interaction_blocked"
RUNTIME_DEBUG_ENV = "TTC_DEBUG"
_INTERACTION_BLOCKED_FEATURE_TEXT = (
    "sorry, this feature isn't available right now"
)


def _runtime_debug_enabled():
    """Enable runtime diagnostics without adding debug to the normal wizard."""
    return str(os.environ.get(RUNTIME_DEBUG_ENV, "")).strip().casefold() in {
        "1", "true", "yes", "on",
    }
_INTERACTION_BLOCKED_SECURITY_TEXT = (
    "a security check is required to proceed"
)


def _normalize_facebook_interaction_error_text(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(str.maketrans({
        "’": "'", "‘": "'", "`": "'", "´": "'",
        "‐": " ", "‑": " ", "‒": " ", "–": " ", "—": " ", "―": " ",
    }))
    return " ".join(text.casefold().split())


def _facebook_interaction_block_message(errors):
    """Return the precise Facebook security-check error, otherwise None."""
    if not isinstance(errors, list):
        return None
    for error in errors:
        if not isinstance(error, dict):
            continue
        parts = [
            str(error.get(field) or "").strip()
            for field in ("message", "summary", "description")
        ]
        parts = [part for part in parts if part]
        combined = " ".join(parts)
        normalized = _normalize_facebook_interaction_error_text(combined)
        if (
            _INTERACTION_BLOCKED_FEATURE_TEXT in normalized
            and _INTERACTION_BLOCKED_SECURITY_TEXT in normalized
        ):
            return combined
    return None


def _interaction_blocked_status(blocked_at):
    return (
        "Tài khoản bị block tương tác lúc "
        f"{blocked_at.strftime('%d/%m/%Y %H:%M:%S')}"
    )


class FacebookInteractionBlocked(RuntimeError):
    """Account-fatal Facebook auto-action security check for this run only."""

    classification = INTERACTION_BLOCKED

    def __init__(self, response_message, blocked_at=None):
        self.response_message = str(response_message or "").strip()
        self.blocked_at = blocked_at or datetime.now().astimezone()
        if not isinstance(self.blocked_at, datetime):
            raise TypeError("blocked_at must be a datetime")
        super().__init__(_interaction_blocked_status(self.blocked_at))

# =====================================================================
# TTC API implementation
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
}

TTC_AUTH_URL = f"{TTC_BASE_URL}/logintoken.php"
TTC_SET_UID_URL = f"{TTC_BASE_URL}/cauhinh/datnick.php"

_TTC_NICKCHAY_JOB_TYPES = frozenset({
    "ttc_like_post_vip_cheo",
    "ttc_reaction_vip_cheo",
    "ttc_reaction_vip_re",
    "ttc_comment",
})

# =====================================================================
# Năm loại job TTC được hỗ trợ
# =====================================================================

ALL_JOB_TYPES = list(TTC_ENDPOINTS.keys())

_APPLICATION_JOB_TYPES = {
    "ttc_like_post_vip_cheo",
    "ttc_like_post_vip_re",
    "ttc_reaction_vip_cheo",
    "ttc_reaction_vip_re",
    "ttc_comment",
}

_VIP_JOB_TYPES = {
    "ttc_like_post_vip_cheo",
    "ttc_like_post_vip_re",
    "ttc_reaction_vip_cheo",
    "ttc_reaction_vip_re",
}

# =====================================================================
# Nhóm action_group
# =====================================================================

ACTION_GROUP_MAP = {
    "ttc_like_post_vip_cheo": "reaction",
    "ttc_like_post_vip_re": "reaction",
    "ttc_reaction_vip_cheo": "reaction",
    "ttc_reaction_vip_re": "reaction",
    "ttc_comment": "comment",
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
}

# =====================================================================
# Reaction type mapping TTC -> nội bộ
# =====================================================================

VALID_REACTIONS = {"LIKE", "LOVE", "CARE", "HAHA", "WOW", "SAD", "ANGRY"}


def _strict_required_text(value, field_name: str) -> str:
    """Validate one required external text field without scalar coercion."""
    if not isinstance(value, str):
        raise ValueError(f"invalid {field_name} type={type(value).__name__}")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"missing {field_name}")
    return normalized


def normalize_reaction_type(raw: str) -> str:
    """Chuẩn hóa reaction type từ dữ liệu TTC về dạng nội bộ.

    TTC trả về loaicx như: LIKE, LOVE, CARE, HAHA, WOW, SAD, ANGRY.
    Trả về chuỗi rỗng nếu giá trị không được hỗ trợ.
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    upper = value.upper()
    if upper in VALID_REACTIONS:
        return upper
    return ""


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

_UNICODE_ESCAPE_RE = re.compile(r"(?:\\u[0-9a-fA-F]{4})+")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")


def _unwrap_ttc_comment_container(value: str, max_depth=3) -> str:
    """Unwrap only whole valid JSON string/array representations."""
    current = value
    for _ in range(max_depth):
        if isinstance(current, list):
            selected = next(
                (
                    item
                    for item in current
                    if isinstance(item, str) and item.strip()
                ),
                "",
            )
            current = selected
            continue

        if not isinstance(current, str):
            return ""

        try:
            decoded = json.loads(current)
        except (TypeError, ValueError, json.JSONDecodeError):
            break
        if isinstance(decoded, (str, list)):
            current = decoded
            continue
        break

    return current if isinstance(current, str) else ""


def _decode_literal_unicode_escapes(value: str) -> str:
    def replace(match):
        try:
            return json.loads(f'"{match.group(0)}"')
        except (TypeError, ValueError, json.JSONDecodeError):
            return match.group(0)

    return _UNICODE_ESCAPE_RE.sub(replace, value)


def parse_ttc_comment_text(nd: str) -> str:
    """Decode TTC Comment wrappers/escapes without rewriting its semantics."""
    if not isinstance(nd, str) or not nd:
        return ""

    text = _unwrap_ttc_comment_container(nd)
    text = _repair_mojibake_if_needed(text)

    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded

    text = _unwrap_ttc_comment_container(text)
    text = _decode_literal_unicode_escapes(text)

    if _PERCENT_ESCAPE_RE.search(text):
        try:
            text = unquote(text, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            pass

    return unicodedata.normalize("NFC", text).strip()


# =====================================================================
# Normalize raw TTC job -> schema nội bộ
# =====================================================================

def normalize_object_id(object_id):
    """Chuẩn hóa ID Facebook từ URL hoặc ID thô."""
    from urllib.parse import parse_qs, urlparse

    if not isinstance(object_id, str):
        return ""
    value = object_id.strip()
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

    Raises:
        TypeError: Raw job không phải dictionary.
        ValueError: Job thiếu dữ liệu cần để execute hoặc claim.
    """
    if not isinstance(raw_data, dict):
        raise TypeError("job must be a dict")

    application_job = job_type in _APPLICATION_JOB_TYPES
    raw_idpost = raw_data.get("idpost")
    raw_link = raw_data.get("link")
    if application_job:
        idpost = _strict_required_text(raw_idpost, "idpost")
        if raw_link is None:
            raise ValueError("missing object_id")
        link = _strict_required_text(raw_link, "link")
    else:
        idpost = raw_idpost.strip() if isinstance(raw_idpost, str) else ""
        link = raw_link.strip() if isinstance(raw_link, str) else ""
    post_id = ""
    if job_type in TTC_DIRECT_POST_ID_JOB_TYPES:
        raw_post_id = raw_data.get("idfb")
        if isinstance(raw_post_id, str):
            candidate_post_id = raw_post_id.strip()
            if re.fullmatch(r"[0-9]+", candidate_post_id) is not None:
                post_id = candidate_post_id
    action_group = get_action_group(job_type)

    # Supported application jobs keep claim ID and executor target independent.
    object_id = _normalize_object_id_impl(link) if link else ""
    if application_job and not object_id:
        raise ValueError("missing object_id")
    if not object_id:
        object_id = idpost
    if not object_id:
        return {}

    # Reaction type (chỉ dùng cho reaction group)
    reaction_type = ""
    if action_group == "reaction":
        # LIKE_POST là hành động LIKE theo contract, không phụ thuộc loaicx.
        if job_type in ("ttc_like_post_vip_cheo", "ttc_like_post_vip_re"):
            reaction_type = "LIKE"
        else:
            reaction_type = normalize_reaction_type(raw_data.get("loaicx"))
            if not reaction_type:
                raise ValueError("missing/unsupported loaicx")

    # Comment text
    comment_text = ""
    if action_group == "comment":
        comment_text = parse_ttc_comment_text(raw_data.get("nd"))
        if not comment_text:
            raise ValueError("missing comment_text")

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
        **({"post_id": post_id} if job_type in TTC_DIRECT_POST_ID_JOB_TYPES else {}),
        "idpost": idpost,            # giữ để gọi nhantien.php
        "raw_data": dict(raw_data),  # debug only, không để GUI phụ thuộc
    }


def normalize_jobs_list(job_type: str, raw_list: list) -> list:
    """Normalize danh sách raw jobs, fail closed nếu có job không hợp lệ."""
    result = []
    for index, raw in enumerate(raw_list):
        try:
            job = normalize_job(job_type, raw)
        except (KeyError, TypeError, ValueError) as exc:
            reason = str(exc).strip()
            if reason == "missing/unsupported loaicx":
                raise TTCResponseError(
                    f"[TTC] Malformed TTC reaction job at index {index}: {reason}."
                ) from exc
            if not any(reason == allowed or reason.startswith(allowed + " type=") for allowed in {
                "job must be a dict",
                "missing idpost",
                "invalid idpost",
                "missing object_id",
                "invalid object_id",
                "missing link",
                "invalid link",
                "missing comment_text",
            }):
                reason = "invalid normalized fields"
            type_name = getattr(job_type, "value", job_type)
            raise TTCResponseError(
                f"[TTC] Malformed TTC job at index {index} "
                f"({type_name}): {reason}."
            ) from exc
        if not isinstance(job, dict) or not job:
            raise TTCResponseError(f"[TTC] Malformed TTC job at index {index}.")
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
    server_raw_text: str = ""
    content_type: str = ""

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
            "server_raw_text": self.server_raw_text,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class FacebookReactionAttemptResult:
    success: bool
    classification: str
    summary: str = ""
    description: str = ""
    raw_text: str = ""


logger = logging.getLogger(__name__)

# =====================================================================
# Custom exceptions
# =====================================================================


class TTCError(Exception):
    """Base TTC error with immutable response evidence when HTTP completed."""

    def __init__(
        self,
        message="",
        *,
        job_type="",
        endpoint="",
        server_raw_text="",
        server_text="",
        http_status=None,
        content_type="",
    ):
        super().__init__(message)
        self.job_type = job_type
        self.endpoint = endpoint
        self.server_raw_text = server_raw_text
        self.server_text = server_text
        self.http_status = http_status
        self.content_type = content_type


class TTCAuthError(TTCError):
    """Token TTC không hợp lệ hoặc đã hết hạn."""


class TTCNoJobAvailable(TTCError):
    """Endpoint không có job nào phù hợp (không phải lỗi API thật)."""

    def __init__(self, message="", countdown=0, **metadata):
        super().__init__(message, **metadata)
        self.ttc_message = message
        parsed_countdown = _parse_strict_integer(countdown)
        self.countdown = max(0, parsed_countdown) if parsed_countdown is not None else 0


class TTCRateLimitError(TTCError):
    """Server TTC đang rate limit request."""


class TTCVipIneligibleError(TTCError):
    """Facebook UID is not eligible for TTC VIP jobs."""


class TTCResponseError(TTCError):
    """Response không đúng format hoặc HTTP lỗi."""


class TTCJobCooldown(TTCError):
    """One exact TTC job endpoint must wait before it can be fetched again."""

    def __init__(self, message="", retry_after=0, **metadata):
        super().__init__(message, **metadata)
        parsed = _parse_strict_integer(retry_after)
        self.retry_after = max(0, parsed) if parsed is not None else 0


# =====================================================================
# Helpers nội bộ
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
    """Che token, chỉ log hash ngắn."""
    if not token or len(token) < 8:
        return "***"
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"***[hash:{h}]"


_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)\b(access_token|authorization|cookie|session|token|password|pass|auth|2fa|secret)\b\s*[:=]\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&<>]+)"
)
_SENSITIVE_JSON_FIELD_RE = re.compile(
    r"(?i)([\"'](?:access_token|authorization|cookie|session|token|password|pass|auth|2fa|secret)[\"']"
    r"\s*:\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&<>}]+)"
)
_MOJIBAKE_MARKERS = (
    "Ãƒ", "Ã‚", "Ã„", "Ã¡Âº", "Ã¡Â»", "chÃƒ", "Ã„â€˜", "Ã†", "Ã",
    "Â", "â€", "â€™", "â€œ", "â€�", "ðŸ", "ï¿½",
)


def _mask_sensitive_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?im)\b(authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]*",
        lambda match: f"{match.group(1)}=***",
        text,
    )
    text = _SENSITIVE_JSON_FIELD_RE.sub(lambda m: f'{m.group(1)}"***"', text)
    text = _SENSITIVE_FIELD_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = re.sub(r"(?i)(access_token|cookie|session|token)=\S+", r"\1=***", text)
    text = re.sub(
        r"(?i)\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 ***",
        text,
    )
    return text


def _mojibake_score(text: str) -> int:
    value = str(text or "")
    marker_score = sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)
    replacement_score = value.count("\ufffd") * 4
    control_score = sum(
        2
        for char in value
        if unicodedata.category(char) == "Cc" and char not in "\n\r\t"
    )
    return marker_score + replacement_score + control_score


def _repair_mojibake_if_needed(text: str) -> str:
    current = str(text or "")
    if _mojibake_score(current) <= 0:
        return current
    best = current
    best_score = _mojibake_score(best)
    for _ in range(2):
        candidates = []
        for source_encoding in ("latin-1", "cp1252"):
            try:
                candidates.append(best.encode(source_encoding).decode("utf-8"))
            except UnicodeError:
                continue
        improved = False
        for candidate in candidates:
            score = _mojibake_score(candidate)
            if score < best_score:
                best = candidate
                best_score = score
                improved = True
        if not improved:
            break
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
    for enc in ("utf-8", charset, response_encoding, apparent_encoding, "latin-1"):
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
    """Parse số xu kiếm được từ message TTC.

    Hỗ trợ dấu chấm và dấu phẩy trong số (vd: 1.000, 1,000).
    """
    if not message:
        return 0
    # Tìm pattern "cộng <số> xu" hoặc "cộng <số>xu"
    # Số có thể chứa dấu . hoặc , ngăn cách hàng nghìn
    folded = _strip_accents(message).casefold()
    match = re.search(r"cong\s+([\d.,]+)\s*xu", folded, flags=re.IGNORECASE | re.UNICODE)
    if not match:
        match = re.search(r"c\S*ng\s+([\d.,]+)\s*xu", str(message or "").lower(), flags=re.IGNORECASE | re.UNICODE)
    if not match:
        # Thử tìm số ngay sau "cộng "
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
    """Lấy giá trị countdown từ response dict."""
    if not isinstance(data, dict):
        return 0
    for key in ("countdown", "time", "wait"):
        if key in data:
            parsed = _parse_strict_integer(data.get(key))
            return max(0, parsed) if parsed is not None else 0
    return 0


def _parse_strict_integer(value):
    """Return an integer only for int (not bool) or integer-like text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def _extract_message(data) -> str:
    """Extract only evidenced text message shapes from TTC responses."""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return ""
    for key in ("mess", "message", "msg", "notice", "status_text", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = data.get("data")
    if isinstance(nested, (dict, str)):
        message = _extract_message(nested)
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


def _is_vip_ineligible_message(message: str) -> bool:
    normalized = " ".join(str(message or "").casefold().split())
    return "chưa đủ điều kiện làm nhiệm vụ vip" in normalized


def _job_cooldown_seconds(message: str) -> int:
    """Match only the observed TTC 'Lấy nhiệm vụ sau <N> giây' contract."""
    normalized = " ".join(_strip_accents(str(message or "")).casefold().split())
    match = re.search(r"\blay nhiem vu sau\s+(\d+)\s+giay\b", normalized)
    return int(match.group(1)) if match else 0


def _server_evidence_message(summary, job_type, server_raw_text, classification):
    return (
        f"{summary}\n"
        f"[TTC SERVER][{job_type}]\n{server_raw_text}\n"
        f"[TTC CLASS]\n{classification}"
    )


def _fetch_semantic_text(data) -> str:
    parts = [_extract_message(data)]
    if isinstance(data, dict):
        error_value = data.get("error")
        if isinstance(error_value, str):
            parts.append(error_value.strip())
        elif isinstance(error_value, (dict, list)):
            parts.append(_extract_message(error_value))
    return " ".join(part for part in parts if part)


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


_SUCCESS_STATUS_VALUES = {"true", "ok", "success", "1"}
_FAILURE_STATUS_VALUES = {"false", "fail", "failed", "error", "rejected", "0"}


def _has_meaningful_error_value(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "none", "null")
    if isinstance(value, dict):
        return any(_has_meaningful_error_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_meaningful_error_value(item) for item in value)
    return True


def _dict_failure_flag(data) -> bool:
    """Detect explicit failure in any structured response branch."""
    if isinstance(data, list):
        return any(_dict_failure_flag(item) for item in data)
    if not isinstance(data, dict):
        return False

    if "error" in data and _has_meaningful_error_value(data.get("error")):
        return True

    for key in ("success", "ok", "status"):
        if key not in data:
            continue
        value = data.get(key)
        if value is False:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
            return True
        if isinstance(value, str) and value.strip().casefold() in _FAILURE_STATUS_VALUES:
            return True

    return any(
        _dict_failure_flag(value)
        for value in data.values()
        if isinstance(value, (dict, list))
    )


def _dict_unknown_result_flag(data) -> bool:
    """Reject structured result flags whose semantics are not recognized."""
    if isinstance(data, list):
        return any(_dict_unknown_result_flag(item) for item in data)
    if not isinstance(data, dict):
        return False

    for key in ("success", "ok", "status"):
        if key not in data:
            continue
        value = data.get(key)
        if value is True or value is False:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
            continue
        if isinstance(value, str):
            folded = value.strip().casefold()
            if folded in _SUCCESS_STATUS_VALUES or folded in _FAILURE_STATUS_VALUES:
                continue
        return True

    return any(
        _dict_unknown_result_flag(value)
        for value in data.values()
        if isinstance(value, (dict, list))
    )


def _classify_claim_response(
    data,
    message: str,
    http_status=None,
    endpoint_key="",
    evidence="",
    raw_summary="",
    server_raw_text="",
    content_type="",
) -> TTCClaimResult:
    clean_message = _safe_display_message(message)
    folded = _strip_accents(clean_message).casefold()
    claim_error_message = ""
    claim_error_folded = ""
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        claim_error_message = _safe_display_message(data["error"])
        claim_error_folded = _strip_accents(claim_error_message).casefold()
    response_kind, response_keys = _response_kind_and_keys(data)
    success_flag = _dict_success_flag(data)
    failure_flag = _dict_failure_flag(data)
    unknown_result_flag = _dict_unknown_result_flag(data)
    parsed_coin = _parse_earned_coin(clean_message)
    has_success_text = "thanh cong" in folded or re.search(r"cong\s+[\d.,]+\s*xu", folded) or parsed_coin > 0
    has_error_text = any(
        marker in folded
        for marker in (
            "that bai", "loi", "khong thanh cong", "chua", "khong tim thay",
            "het han", "dang nhap", "huy", "sai", "invalid",
            "error", "failed", "failure", "rejected",
        )
    )
    kwargs = {
        "http_status": http_status,
        "endpoint_key": endpoint_key,
        "evidence": evidence,
        "response_kind": response_kind,
        "response_keys": response_keys,
        "raw_summary": _safe_display_message(raw_summary or evidence, limit=300),
        "server_raw_text": server_raw_text,
        "content_type": content_type,
    }
    if response_kind == "html":
        return TTCClaimResult(
            False,
            "INVALID_RESPONSE",
            clean_message or "TTC trả về HTML thay vì dữ liệu claim",
            retryable=False,
            **kwargs,
        )
    if (any(marker in folded for marker in (
            "chua lam", "chua tuong tac", "chua xac nhan", "chua thay",
            "chua like", "not observed",
        )) or "chua like" in claim_error_folded):
        return TTCClaimResult(
            False,
            "NOT_OBSERVED_YET",
            clean_message or claim_error_message,
            retryable=True,
            **kwargs,
        )
    if any(marker in folded for marker in ("da nhan", "da duoc nhan", "already")):
        return TTCClaimResult(False, "ALREADY_CLAIMED", clean_message, retryable=False, **kwargs)
    if any(marker in folded for marker in ("dang nhap", "vui long dang nhap", "het han", "login", "unauthorized", "auth")):
        return TTCClaimResult(False, "AUTH_EXPIRED", clean_message, retryable=False, **kwargs)
    if any(marker in folded for marker in ("rate limit", "qua nhanh", "thu lai sau", "cho")):
        return TTCClaimResult(False, "RATE_LIMITED", clean_message, retryable=True, **kwargs)
    if any(marker in folded for marker in ("id", "job", "nhiem vu", "khong ton tai", "huy")):
        return TTCClaimResult(False, "INVALID_JOB", clean_message, retryable=False, **kwargs)
    if failure_flag or has_error_text:
        return TTCClaimResult(
            False,
            "TTC_REJECTED",
            clean_message or "TTC returned an explicit structured failure",
            retryable=False,
            **kwargs,
        )
    if unknown_result_flag:
        return TTCClaimResult(
            False,
            "UNKNOWN_RESPONSE",
            clean_message or "TTC returned an unknown structured result flag",
            retryable=False,
            **kwargs,
        )
    if success_flag or has_success_text:
        return TTCClaimResult(True, "SUCCESS", clean_message, parsed_coin, **kwargs)
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
    """HTTP client TTC cho một worker.

    Mỗi worker tạo một instance riêng — không chia sẻ session/cookie.
    """

    def __init__(self, worker_id="?", log_fn=None):
        """
        Args:
            worker_id: ID worker dùng để prefix log.
            log_fn: callable(message, level) hoặc None (dùng print).
        """
        self.worker_id = str(worker_id)
        self._log_fn = log_fn
        self._session = self._create_session()
        self._token = ""
        self._user = ""
        self._initial_balance = None
        self._authenticated = False
        self._configured_facebook_uid = None

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
        elif level == "account":
            print(f"{Fore.MAGENTA}{Style.BRIGHT}👤 {message}{Style.RESET_ALL}")
        else:
            print(f"[TTC Worker #{self.worker_id}] [{level.upper()}] {message}")

    def _headers(self) -> dict:
        return dict(_HEADERS_BASE)

    # ------------------------------------------------------------------
    # authenticate()
    # ------------------------------------------------------------------

    def authenticate(self, token: str) -> None:
        """Đăng nhập TTC bằng access_token.

        Args:
            token: TTC token (không log đầy đủ).

        Raises:
            TTCAuthError: Token sai hoặc response không hợp lệ.
            TTCResponseError: Response không parse được.
            TTCRateLimitError: Rate limit từ server.
        """
        self._token = ""
        self._user = ""
        self._initial_balance = None
        self._authenticated = False
        self._configured_facebook_uid = None

        cookies = getattr(self._session, "cookies", None)
        clear_cookies = getattr(cookies, "clear", None)
        if callable(clear_cookies):
            try:
                clear_cookies()
            except Exception as exc:
                raise TTCResponseError(
                    "[TTC] Cannot clear stale session cookies before authentication."
                ) from exc

        if not token or not str(token).strip():
            raise TTCAuthError("[TTC] Token rỗng, không thể đăng nhập.")

        token_value = str(token).strip()

        try:
            resp = self._session.post(
                TTC_AUTH_URL,
                data={"access_token": token_value},
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
                f"[TTC] Authentication response field 'data' must be an object; "
                f"received {type(raw_data).__name__}."
            )

        user_field = "user" if "user" in raw_data else "username"
        try:
            user = _strict_required_text(raw_data.get(user_field), user_field)
        except ValueError as exc:
            raise TTCAuthError(
                f"[TTC] Authentication response field '{user_field}' must be non-empty text."
            ) from exc

        if "sodu" not in raw_data or raw_data["sodu"] is None:
            raise TTCResponseError(
                "[TTC] Authentication response is missing required field 'sodu'."
            )

        balance = _parse_strict_integer(raw_data["sodu"])
        if balance is None:
            raise TTCResponseError(
                "[TTC] Authentication response field 'sodu' is not a valid integer."
            )

        self._token = token_value
        self._user = user
        self._initial_balance = balance
        self._authenticated = True

        self._log(
            f"TTC: {self._user} | {format_coin(balance)} xu",
            "account",
        )

    # ------------------------------------------------------------------
    # configure_facebook_uid()
    # ------------------------------------------------------------------

    def bind_facebook_uid(self, uid: str) -> None:
        """Bind a Facebook UID to this client's GET/CLAIM requests locally."""
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi bind UID.")
        self._configured_facebook_uid = None
        try:
            uid = _strict_required_text(uid, "Facebook UID")
        except ValueError as exc:
            raise ValueError("[TTC] UID Facebook phải là chuỗi không rỗng.") from exc
        self._configured_facebook_uid = uid

    def configure_facebook_uid(self, uid: str) -> None:
        """Đặt UID Facebook cho tài khoản TTC.

        Chỉ gọi sau khi authenticate() thành công.

        Raises:
            TTCAuthError: Chưa đăng nhập.
            TTCResponseError: Request lỗi hoặc server không xác nhận.
        """
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi đặt UID.")

        # A new configuration attempt invalidates the previous binding until
        # TTC confirms this UID with the observed exact response contract.
        self._configured_facebook_uid = None
        try:
            uid = _strict_required_text(uid, "Facebook UID")
        except ValueError as exc:
            raise ValueError("[TTC] UID Facebook phải là chuỗi không rỗng.") from exc
        try:
            resp = self._session.post(
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

        if resp.status_code == 401 or resp.status_code == 403:
            raise TTCAuthError(
                f"[TTC] HTTP {resp.status_code}: Phiên đăng nhập có thể đã hết hạn khi đặt UID."
            )
        if resp.status_code == 429:
            raise TTCRateLimitError("[TTC] Server TTC rate limit khi đặt UID Facebook.")
        if resp.status_code >= 500:
            raise TTCResponseError(
                f"[TTC] Server TTC lỗi {resp.status_code} khi đặt UID Facebook."
            )
        if resp.status_code >= 400:
            raise TTCResponseError(
                f"[TTC] HTTP {resp.status_code} khi đặt UID Facebook."
            )

        body = (resp.text or "").strip()
        if body != "1":
            raise TTCResponseError(
                f"[TTC] Server không xác nhận UID Facebook. Response: {body[:200]!r}"
            )

        self.bind_facebook_uid(uid)

    # ------------------------------------------------------------------
    # get_account_info()
    # ------------------------------------------------------------------

    def get_account_info(self) -> dict:
        """Lấy thông tin user và số dư từ session hiện tại."""
        return {
            "user": self._user,
            "balance": self._initial_balance,
            "authenticated": self._authenticated,
        }

    # ------------------------------------------------------------------
    # fetch_jobs()
    # ------------------------------------------------------------------

    def fetch_jobs(self, job_type: str) -> list:
        """Lấy danh sách job từ endpoint TTC.

        Args:
            job_type: Một trong ALL_JOB_TYPES.

        Returns:
            List các job đã normalize, sẵn sàng đưa vào JobQueue.

        Raises:
            TTCNoJobAvailable: Không có job (không phải lỗi API).
            TTCAuthError: Phiên đã hết hạn.
            TTCRateLimitError: Rate limit.
            TTCResponseError: Lỗi HTTP hoặc format.
            ValueError: job_type không hợp lệ.
        """
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi fetch_jobs.")
        if self._configured_facebook_uid is None:
            raise TTCResponseError(
                "[TTC] Chưa bind UID Facebook cho request trước khi fetch_jobs."
            )
        if job_type not in TTC_ENDPOINTS:
            raise ValueError(f"[TTC] job_type không hợp lệ: {job_type!r}")

        get_url, _ = TTC_ENDPOINTS[job_type]
        job_type_key = job_type.value if isinstance(job_type, Enum) else str(job_type)

        request_kwargs = {
            "headers": self._headers(),
            "timeout": 15,
        }
        if job_type in _TTC_NICKCHAY_JOB_TYPES:
            request_kwargs["params"] = {
                "nickchay": self._configured_facebook_uid,
            }

        try:
            resp = self._session.get(get_url, **request_kwargs)
        except requests.exceptions.Timeout as e:
            raise TTCResponseError(
                f"[TTC] Timeout khi lấy job {job_type}: {e}",
                job_type=job_type_key,
                endpoint=get_url,
            ) from e
        except requests.exceptions.RequestException as e:
            raise TTCResponseError(
                f"[TTC] Lỗi mạng khi lấy job {job_type}: {e}",
                job_type=job_type_key,
                endpoint=get_url,
            ) from e

        decoded_text, decode_meta = _decode_response_body(resp)
        content_type = decode_meta.get("content_type", "")
        response_metadata = {
            "job_type": job_type_key,
            "endpoint": get_url,
            "server_raw_text": decoded_text,
            "http_status": resp.status_code,
            "content_type": content_type,
        }
        if resp.status_code == 429:
            raise TTCRateLimitError(
                _server_evidence_message(
                    f"[TTC] Rate limit khi lấy job {job_type}.",
                    job_type_key,
                    decoded_text,
                    "RATE_LIMIT",
                ),
                **response_metadata,
            )
        if resp.status_code == 401 or resp.status_code == 403:
            raise TTCAuthError(
                _server_evidence_message(
                    f"[TTC] HTTP {resp.status_code}: Phiên đăng nhập có thể đã hết hạn.",
                    job_type_key,
                    decoded_text,
                    "AUTH_ERROR",
                ),
                **response_metadata,
            )
        if resp.status_code >= 500:
            raise TTCResponseError(
                _server_evidence_message(
                    f"[TTC] Server lỗi {resp.status_code} khi lấy job {job_type}.",
                    job_type_key,
                    decoded_text,
                    "RESPONSE_ERROR",
                ),
                **response_metadata,
            )
        if resp.status_code >= 400:
            raise TTCResponseError(
                _server_evidence_message(
                    f"[TTC] HTTP {resp.status_code} khi lấy job {job_type}.",
                    job_type_key,
                    decoded_text,
                    "RESPONSE_ERROR",
                ),
                **response_metadata,
            )

        # Parse fetch responses using the endpoint's fail-closed contract.
        if not decoded_text.strip():
            raise TTCResponseError(
                _server_evidence_message(
                    f"[TTC] Empty response from {job_type}.",
                    job_type_key,
                    decoded_text,
                    "RESPONSE_ERROR",
                ),
                **response_metadata,
            )

        try:
            data = json.loads(decoded_text)
        except Exception:
            # Không phải JSON — có thể là HTML lỗi hoặc empty
            snippet = _safe_display_message(decoded_text, limit=200)
            raise TTCResponseError(
                _server_evidence_message(
                    f"[TTC] Response không phải JSON từ {job_type}. Snippet: {snippet!r}",
                    job_type_key,
                    decoded_text,
                    "RESPONSE_ERROR",
                ),
                **response_metadata,
            )

        semantic_text = _fetch_semantic_text(data)
        # Case 1: List có job
        if (
            job_type in _VIP_JOB_TYPES
            and _is_vip_ineligible_message(semantic_text)
        ):
            raise TTCVipIneligibleError(
                _server_evidence_message(
                    "[TTC] Facebook UID is not eligible for this VIP endpoint.",
                    job_type_key,
                    decoded_text,
                    "VIP_INELIGIBLE",
                ),
                server_text=semantic_text,
                **response_metadata,
            )

        retry_after = _job_cooldown_seconds(semantic_text)
        if retry_after > 0:
            raise TTCJobCooldown(
                _server_evidence_message(
                    f"[TTC] Endpoint cooldown while fetching {job_type}.",
                    job_type_key,
                    decoded_text,
                    f"TEMP_COOLDOWN retry_after={retry_after}",
                ),
                retry_after=retry_after,
                server_text=semantic_text,
                **response_metadata,
            )

        if isinstance(data, list) and len(data) > 0:
            jobs = normalize_jobs_list(job_type, data)
            for job in jobs:
                job["server_raw_text"] = decoded_text
                job["http_status"] = resp.status_code
                job["content_type"] = content_type
            return jobs

        # Case 2: List rỗng
        if isinstance(data, list) and len(data) == 0:
            raise TTCNoJobAvailable(
                f"[TTC] Không có job {job_type}.",
                0,
                **response_metadata,
            )

        # Case 3: Dict responses fail closed. Error semantics take precedence;
        # countdown alone is not evidence that TTC has no jobs.
        if isinstance(data, dict):
            message = _extract_message(data)
            error_value = data.get("error")
            has_explicit_error = (
                "error" in data and error_value not in (None, "", False)
            )
            if isinstance(error_value, str):
                error_message = error_value.strip()
            elif isinstance(error_value, (dict, list)):
                error_message = _extract_message(error_value)
            else:
                error_message = ""

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
                    _server_evidence_message(
                        f"[TTC] Authentication failure while fetching {job_type}: "
                        f"{display_message or 'authentication error'}",
                        job_type_key,
                        decoded_text,
                        "AUTH_ERROR",
                    ),
                    server_text=semantic_text,
                    **response_metadata,
                )

            if (
                re.search(r"\brate[\s_-]*limit(?:ed|ing)?\b", folded)
                or "too many request" in folded
            ):
                raise TTCRateLimitError(
                    _server_evidence_message(
                        f"[TTC] Rate limit while fetching {job_type}: "
                        f"{display_message or 'rate limited'}",
                        job_type_key,
                        decoded_text,
                        "RATE_LIMIT",
                    ),
                    server_text=semantic_text,
                    **response_metadata,
                )

            if has_explicit_error:
                raise TTCResponseError(
                    _server_evidence_message(
                        f"[TTC] API error while fetching {job_type}: "
                        f"{_safe_display_message(error_message or message) or 'explicit error'}",
                        job_type_key,
                        decoded_text,
                        "RESPONSE_ERROR",
                    ),
                    server_text=semantic_text,
                    **response_metadata,
                )

            _response_kind, response_keys = _response_kind_and_keys(data)
            raise TTCResponseError(
                _server_evidence_message(
                    f"[TTC] Unknown response dict from {job_type}. "
                    f"Keys={','.join(response_keys) or '-'}; "
                    f"Message={_safe_display_message(message) or '-'}",
                    job_type_key,
                    decoded_text,
                    "RESPONSE_ERROR",
                ),
                server_text=semantic_text,
                **response_metadata,
            )

        raise TTCResponseError(
            _server_evidence_message(
                f"[TTC] Response format không xác định từ {job_type}: {type(data).__name__}",
                job_type_key,
                decoded_text,
                "RESPONSE_ERROR",
            ),
            **response_metadata,
        )

    # ------------------------------------------------------------------
    # claim_reward()
    # ------------------------------------------------------------------

    def claim_reward(self, job: dict) -> dict:
        """Nhận xu sau khi thao tác Facebook thành công.

        Chỉ gọi hàm này sau khi xác nhận Facebook action = True.

        Args:
            job: Dict job nội bộ đã normalize.

        Returns:
            {
                "success": bool,
                "message": str,
                "earned_coin": int,
                "balance": int | None,
            }

        Raises:
            TTCAuthError: Phiên hết hạn.
            TTCResponseError: Lỗi mạng/HTTP.
        """
        if not self._authenticated:
            raise TTCAuthError("[TTC] Chưa đăng nhập trước khi claim_reward.")

        job_type = job.get("type", "")
        if job_type not in TTC_ENDPOINTS:
            raise ValueError(f"[TTC] job_type không hợp lệ khi claim: {job_type!r}")

        _, claim_url = TTC_ENDPOINTS[job_type]
        job_type_key = job_type.value if isinstance(job_type, Enum) else str(job_type)
        try:
            idpost = _strict_required_text(job.get("idpost"), "claim idpost")
        except (AttributeError, ValueError) as exc:
            raise ValueError("[TTC] claim idpost phải là chuỗi không rỗng.") from exc

        # Payload theo loại job
        payload = {"id": idpost}
        if job_type in ("ttc_reaction_vip_cheo", "ttc_reaction_vip_re"):
            reaction_type = job.get("reaction_type")
            if not isinstance(reaction_type, str) or reaction_type not in VALID_REACTIONS:
                raise ValueError("[TTC] claim reaction_type phải là canonical reaction string.")
            payload["loaicx"] = reaction_type
        if job_type in _TTC_NICKCHAY_JOB_TYPES:
            if self._configured_facebook_uid is None:
                raise TTCResponseError(
                    "[TTC] Chưa bind UID Facebook cho request trước khi claim_reward."
                )
            payload["nickchay"] = self._configured_facebook_uid

        try:
            resp = self._session.post(
                claim_url,
                headers=self._headers(),
                data=payload,
                timeout=15,
            )
        except requests.exceptions.Timeout as e:
            raise TTCResponseError(
                f"[TTC] Timeout khi claim reward job_type={job_type}: {e}",
                job_type=job_type_key,
                endpoint=claim_url,
            ) from e
        except requests.exceptions.RequestException as e:
            raise TTCResponseError(
                f"[TTC] Lỗi mạng khi claim reward job_type={job_type}: {e}",
                job_type=job_type_key,
                endpoint=claim_url,
            ) from e

        decoded_text, decode_meta = _decode_response_body(resp)
        content_type = decode_meta.get("content_type", "") or (
            resp.headers.get("content-type", "")
            if getattr(resp, "headers", None)
            else ""
        )
        response_metadata = {
            "job_type": job_type_key,
            "endpoint": claim_url,
            "server_raw_text": decoded_text,
            "http_status": resp.status_code,
            "content_type": content_type,
        }
        if resp.status_code == 401 or resp.status_code == 403:
            raise TTCAuthError(
                _server_evidence_message(
                    f"[TTC] HTTP {resp.status_code}: Phiên đăng nhập hết hạn khi claim.",
                    job_type_key,
                    decoded_text,
                    "AUTH_ERROR",
                ),
                **response_metadata,
            )
        if resp.status_code == 429:
            raise TTCRateLimitError(
                _server_evidence_message(
                    f"[TTC] Rate limit khi claim reward job_type={job_type}.",
                    job_type_key,
                    decoded_text,
                    "RATE_LIMIT",
                ),
                **response_metadata,
            )
        if resp.status_code >= 400:
            raise TTCResponseError(
                _server_evidence_message(
                    f"[TTC] HTTP {resp.status_code} khi claim reward job_type={job_type}.",
                    job_type_key,
                    decoded_text,
                    "CLAIM_REJECTED",
                ),
                **response_metadata,
            )

        data = _safe_json(resp, f"claim_reward({job_type})", allow_plain_text=True)
        message = _extract_message(data)
        evidence = _summarize_claim_response(
            data,
            response_text=decoded_text,
            http_status=resp.status_code,
            content_type=content_type,
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
            server_raw_text=decoded_text,
            content_type=content_type,
        )

        # Đọc số dư nếu có
        balance = None
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            raw_balance = data["data"].get("sodu")
            balance = _parse_strict_integer(raw_balance)

        claim.balance = balance
        if claim.success:
            if balance is not None:
                self._initial_balance = balance
            elif (isinstance(self._initial_balance, int)
                    and not isinstance(self._initial_balance, bool)
                    and isinstance(claim.earned_coin, int)
                    and not isinstance(claim.earned_coin, bool)):
                self._initial_balance += claim.earned_coin
        return claim.to_dict()

    # ------------------------------------------------------------------
    # close()
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Đóng HTTP session."""
        try:
            self._session.close()
        except Exception:
            pass
        self._authenticated = False
        self._configured_facebook_uid = None
        self._token = ""
        self._user = ""
        self._initial_balance = None

# =====================================================================
# Tên gọi thuận tiện khi tích hợp sang tool khác
# =====================================================================

class TTCJobType(str, Enum):
    LIKE_POST_VIP_CHEO = "ttc_like_post_vip_cheo"
    LIKE_POST_VIP_RE = "ttc_like_post_vip_re"
    REACTION_VIP_CHEO = "ttc_reaction_vip_cheo"
    REACTION_VIP_RE = "ttc_reaction_vip_re"
    COMMENT = "ttc_comment"


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


# =====================================================================
# TTC scheduler compatibility adapter
# =====================================================================

class TTCFetchState:
    """Non-persistent endpoint state shared only by one profile run.

    Temporary TTC endpoint cooldowns belong to the profile run, while VIP
    eligibility and endpoint rotation remain scoped to each Facebook UID.
    """

    def __init__(self):
        self.permanent_skip = set()
        self.temporary_retry_at = {}
        self.endpoint_cursor = {}
        # Session cache: tái sử dụng cookie TTC giữa các account Facebook
        # trong cùng một profile run, tránh POST login lặp lại.
        self.cached_cookies = None   # dict hoặc None
        self.cached_user = None      # str hoặc None
        self.cached_balance = None   # int hoặc None


class API_TTC:
    """Expose TTC lifecycle operations through the existing runtime interface."""

    REACTION_JOB_TYPES = (
        TTCJobType.LIKE_POST_VIP_CHEO,
        TTCJobType.LIKE_POST_VIP_RE,
        TTCJobType.REACTION_VIP_CHEO,
        TTCJobType.REACTION_VIP_RE,
    )
    COMMENT_JOB_TYPES = (TTCJobType.COMMENT,)
    LEGACY_JOB_TYPES = {
        "like_facebook": REACTION_JOB_TYPES,
        "like_poster": COMMENT_JOB_TYPES,
    }

    def __init__(self, access_token, worker_id="?", fetch_state=None, log_fn=None):
        self._access_token = access_token
        self._client = TTCClient(worker_id=worker_id, log_fn=log_fn)
        self._fetch_state = fetch_state if fetch_state is not None else TTCFetchState()
        self._facebook_uid = None

    def login(self):
        fs = self._fetch_state
        if fs.cached_cookies is not None:
            # Nạp lại cookie đã cache vào session mới, bỏ qua POST login
            session_cookies = getattr(self._client._session, "cookies", None)
            if session_cookies is not None and hasattr(session_cookies, "update"):
                session_cookies.update(fs.cached_cookies)
            self._client._token = self._access_token
            self._client._user = fs.cached_user or ""
            self._client._initial_balance = fs.cached_balance
            self._client._authenticated = True
            return {
                "user": fs.cached_user,
                "balance": fs.cached_balance,
            }
        # Lần đầu: thực hiện POST login thật, sau đó lưu vào cache
        result = self._client.login(self._access_token)
        try:
            raw_cookies = self._client._session.cookies
            fs.cached_cookies = dict(raw_cookies) if raw_cookies else {}
        except Exception:
            fs.cached_cookies = {}
        fs.cached_user = self._client._user
        fs.cached_balance = self._client._initial_balance
        return result

    def use_fetch_state(self, fetch_state):
        if not isinstance(fetch_state, TTCFetchState):
            raise TypeError("fetch_state must be TTCFetchState")
        self._fetch_state = fetch_state

    def set_facebook_uid(self, uid):
        result = self._client.set_uid(uid)
        self._facebook_uid = str(uid).strip()
        return result

    def bind_facebook_uid(self, uid):
        result = self._client.bind_facebook_uid(uid)
        self._facebook_uid = str(uid).strip()
        return result

    def fetch_jobs_for_legacy_type(self, legacy_type):
        try:
            job_types = self.LEGACY_JOB_TYPES[legacy_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported legacy job type: {legacy_type!r}") from exc

        no_job = []
        cooldowns = []
        permanent_skips = []
        now = time.monotonic()
        eligible = {}
        for index, job_type in enumerate(job_types):
            endpoint_key = job_type.value
            uid_endpoint_key = (self._facebook_uid, endpoint_key)
            if uid_endpoint_key in self._fetch_state.permanent_skip:
                permanent_skips.append({
                    "job_type": endpoint_key,
                    "source": "local",
                })
                continue

            retry_at = self._fetch_state.temporary_retry_at.get(endpoint_key)
            if retry_at is not None:
                if now < retry_at:
                    cooldowns.append({
                        "job_type": endpoint_key,
                        "remaining": max(1, math.ceil(retry_at - now)),
                        "retry_at": retry_at,
                        "source": "local",
                    })
                    continue
                del self._fetch_state.temporary_retry_at[endpoint_key]

            eligible[index] = (job_type, uid_endpoint_key)

        if not eligible:
            return {
                "jobs": [],
                "no_job": no_job,
                "cooldowns": cooldowns,
                "permanent_skips": permanent_skips,
            }

        cursor_key = (self._facebook_uid, legacy_type)
        start_index = self._fetch_state.endpoint_cursor.get(cursor_key, 0) % len(job_types)

        # Thử từng eligible endpoint theo thứ tự cursor; fallback sang endpoint tiếp
        # nếu gặp Cooldown / VipIneligible / NoJob thay vì bỏ cuộc ngay.
        jobs = []
        tried_any = False
        for offset in range(len(job_types)):
            candidate_index = (start_index + offset) % len(job_types)
            if candidate_index not in eligible:
                continue
            job_type, uid_endpoint_key = eligible[candidate_index]
            endpoint_key = job_type.value
            tried_any = True

            try:
                ttc_jobs = self._client.get_jobs(job_type)
            except TTCJobCooldown as exc:
                retry_at = now + exc.retry_after
                self._fetch_state.temporary_retry_at[endpoint_key] = retry_at
                cooldowns.append({
                    "job_type": endpoint_key,
                    "retry_after": exc.retry_after,
                    "remaining": exc.retry_after,
                    "retry_at": retry_at,
                    "server_raw_text": exc.server_raw_text,
                    "http_status": exc.http_status,
                    "content_type": exc.content_type,
                    "source": "server",
                })
                continue  # thử endpoint tiếp theo
            except TTCVipIneligibleError as exc:
                self._fetch_state.permanent_skip.add(uid_endpoint_key)
                permanent_skips.append({
                    "job_type": endpoint_key,
                    "server_raw_text": exc.server_raw_text,
                    "http_status": exc.http_status,
                    "content_type": exc.content_type,
                    "source": "server",
                })
                continue  # thử endpoint tiếp theo
            except TTCNoJobAvailable as exc:
                no_job.append({
                    "job_type": endpoint_key,
                    "countdown": exc.countdown,
                })
                continue  # thử endpoint tiếp theo

            # Endpoint này thành công — cập nhật cursor rồi gom job
            self._fetch_state.endpoint_cursor[cursor_key] = (candidate_index + 1) % len(job_types)
            for original_job in ttc_jobs:
                scheduler_job = dict(original_job)
                scheduler_job["type"] = legacy_type
                scheduler_job["_ttc_job"] = original_job
                jobs.append(scheduler_job)
            break  # có job rồi, không cần thử tiếp

        return {
            "jobs": jobs,
            "no_job": no_job,
            "cooldowns": cooldowns,
            "permanent_skips": permanent_skips,
        }

    def submit_job(self, job):
        original_job = job.get("_ttc_job", job) if isinstance(job, dict) else job
        return self._client.submit_job(original_job)

    def get_account_info(self):
        return self._client.get_account_info()

    def close(self):
        return self._client.close()


# ---------- Scheduler / delay helpers (ported from PC, kept single-file) ----------
def normalize_job_types(job_types):
    if job_types is None:
        return SUPPORTED_JOB_TYPES
    if isinstance(job_types, str):
        enabled_types = {job_types}
    else:
        try:
            enabled_types = set(job_types)
        except TypeError as exc:
            raise ValueError("job_types must be a job type or collection of job types") from exc

    invalid_types = enabled_types - SUPPORTED_JOB_TYPES
    if invalid_types:
        raise ValueError(f"Unsupported job types: {sorted(invalid_types)}")
    if not enabled_types:
        raise ValueError("At least one job type must be selected")
    return frozenset(enabled_types)


def validate_batch_limit(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be an integer greater than or equal to 1")
    return value


def validate_integer_range(minimum, maximum, label, minimum_allowed):
    if (isinstance(minimum, bool) or isinstance(maximum, bool)
            or not isinstance(minimum, int) or not isinstance(maximum, int)):
        raise ValueError(f"{label} values must be integers")
    if minimum < minimum_allowed or maximum < minimum_allowed:
        raise ValueError(f"{label} values must be greater than or equal to {minimum_allowed}")
    if minimum > maximum:
        raise ValueError(f"{label} minimum must not exceed maximum")
    return minimum, maximum


def _resolve_burst_range(legacy_limit, minimum, maximum, default, label):
    if legacy_limit is not None:
        validate_batch_limit(legacy_limit, f"{label} limit")
        if minimum is not None and minimum != legacy_limit:
            raise ValueError(f"Conflicting legacy and explicit {label} minimum")
        if maximum is not None and maximum != legacy_limit:
            raise ValueError(f"Conflicting legacy and explicit {label} maximum")
        minimum = legacy_limit
        maximum = legacy_limit
    elif minimum is None and maximum is None:
        minimum = default
        maximum = default
    elif minimum is None or maximum is None:
        raise ValueError(f"{label} minimum and maximum must be provided together")
    return validate_integer_range(minimum, maximum, f"{label} batch", 1)


def resolve_burst_ranges(like_batch_limit=None, comment_batch_limit=None,
                         like_batch_min=None, like_batch_max=None,
                         comment_batch_min=None, comment_batch_max=None):
    return (
        _resolve_burst_range(like_batch_limit, like_batch_min, like_batch_max, 10, "like"),
        _resolve_burst_range(comment_batch_limit, comment_batch_min, comment_batch_max, 5, "comment"),
    )


def schedule_jobs_by_burst(jobs, enabled_types, max_jobs,
                           like_batch_limit=None, comment_batch_limit=None, *,
                           like_batch_min=None, like_batch_max=None,
                           comment_batch_min=None, comment_batch_max=None):
    like_range, comment_range = resolve_burst_ranges(
        like_batch_limit, comment_batch_limit,
        like_batch_min, like_batch_max,
        comment_batch_min, comment_batch_max,
    )
    if max_jobs <= 0:
        return []

    queues = {
        'like_facebook': [
            job for job in jobs
            if 'like_facebook' in enabled_types and job.get('type') == 'like_facebook'
        ],
        'like_poster': [
            job for job in jobs
            if 'like_poster' in enabled_types and job.get('type') == 'like_poster'
        ],
    }
    ranges = {'like_facebook': like_range, 'like_poster': comment_range}
    candidate_jobs = []

    for job_type in ('like_facebook', 'like_poster'):
        if job_type not in enabled_types:
            continue
        target = random.randint(*ranges[job_type])
        candidate_jobs.extend(queues[job_type][:target])

    if len(candidate_jobs) > 1:
        random.shuffle(candidate_jobs)
    return candidate_jobs[:max_jobs]


def resolve_delay_ranges(normal_delay_min=10, normal_delay_max=None,
                         failed_delay_min=None, failed_delay_max=None,
                         delay_between_jobs=None):
    if delay_between_jobs is not None:
        normal_delay_min = delay_between_jobs
        normal_delay_max = delay_between_jobs
    if normal_delay_max is None:
        normal_delay_max = normal_delay_min
    if failed_delay_min is None:
        failed_delay_min = normal_delay_min
    if failed_delay_max is None:
        failed_delay_max = normal_delay_max
    normal_range = validate_integer_range(normal_delay_min, normal_delay_max, "normal delay", 0)
    failed_range = validate_integer_range(failed_delay_min, failed_delay_max, "failed delay", 0)
    return normal_range, failed_range


def _resolve_explicit_delay_range(minimum, maximum, fallback_range,
                                  fallback_was_explicit, label):
    if minimum is None and maximum is None:
        return fallback_range
    if minimum is None or maximum is None:
        raise ValueError(f"{label} minimum and maximum must be provided together")
    resolved = validate_integer_range(minimum, maximum, label, 0)
    if fallback_was_explicit and resolved != fallback_range:
        raise ValueError(f"Conflicting legacy normal delay and explicit {label}")
    return resolved


def resolve_run_delay_ranges(normal_delay_min=None, normal_delay_max=None,
                             failed_delay_min=None, failed_delay_max=None,
                             delay_between_jobs=None, like_delay_min=None,
                             like_delay_max=None, comment_delay_min=None,
                             comment_delay_max=None, account_delay_min=None,
                             account_delay_max=None):
    legacy_was_explicit = (
        delay_between_jobs is not None
        or normal_delay_min is not None
        or normal_delay_max is not None
    )
    if delay_between_jobs is not None:
        normal_delay_min = delay_between_jobs
        normal_delay_max = delay_between_jobs
    if normal_delay_min is None:
        normal_delay_min = 10
    if normal_delay_max is None:
        normal_delay_max = normal_delay_min
    normal_range = validate_integer_range(normal_delay_min, normal_delay_max, "normal delay", 0)

    if failed_delay_min is None:
        failed_delay_min = normal_range[0]
    if failed_delay_max is None:
        failed_delay_max = normal_range[1]
    failed_range = validate_integer_range(failed_delay_min, failed_delay_max, "failed delay", 0)

    like_range = _resolve_explicit_delay_range(
        like_delay_min, like_delay_max, normal_range, legacy_was_explicit, "like delay"
    )
    comment_range = _resolve_explicit_delay_range(
        comment_delay_min, comment_delay_max, normal_range, legacy_was_explicit, "comment delay"
    )
    account_range = _resolve_explicit_delay_range(
        account_delay_min, account_delay_max, normal_range, legacy_was_explicit, "account delay"
    )
    return like_range, comment_range, failed_range, account_range


def prompt_positive_int(prompt, default):
    raw_value = input(prompt).strip()
    if not raw_value:
        return default
    try:
        return validate_batch_limit(int(raw_value), "batch limit")
    except ValueError:
        print_warning(f"Giá trị không hợp lệ, dùng mặc định {default}.")
        return default


def prompt_int(prompt, default, minimum=0):
    raw = input(prompt).strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError
        return value
    except ValueError:
        print_warning(f"Giá trị không hợp lệ, dùng {default}.")
        return default


def prompt_range(label, current_min, current_max, minimum_allowed=0):
    while True:
        minimum = prompt_int(f"{label} tối thiểu [{current_min}]: ", current_min, minimum_allowed)
        maximum = prompt_int(f"{label} tối đa [{current_max}]: ", current_max, minimum_allowed)
        try:
            return validate_integer_range(minimum, maximum, label, minimum_allowed)
        except ValueError as exc:
            print_error(str(exc))

# ---------- Màu sắc ----------
def format_coin(value):
    """Format an internal integer for compact Vietnamese coin output."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("coin value must be an integer")
    return f"{value:,}".replace(",", ".")


def print_success(msg):
    print(f"{Fore.GREEN}✅ {msg}{Style.RESET_ALL}")

def print_error(msg):
    print(f"{Fore.RED}❌ {msg}{Style.RESET_ALL}")

def print_info(msg):
    print(f"{Fore.CYAN}ℹ️ {msg}{Style.RESET_ALL}")

def print_warning(msg):
    print(f"{Fore.YELLOW}⚠️ {msg}{Style.RESET_ALL}")


_TTC_JOB_TYPE_LABELS = {
    "ttc_like_post_vip_cheo": "Like_Vip",
    "ttc_like_post_vip_re": "Like_thường",
    "ttc_reaction_vip_cheo": "CX_Vip",
    "ttc_reaction_vip_re": "CX_Vip",
    "ttc_comment": "Comment",
    "like_facebook": "Like",
    "like_poster": "Comment",
}


def compact_ttc_job_type(source):
    """Return a short presentation label for a canonical TTC source."""
    return _TTC_JOB_TYPE_LABELS.get(source, "Unknown")


JOB_TABLE_HEADER = (
    f"| {'STT':<3} | {'UID-TK làm job':<17} | {'ID-post':<18}|"
    f" {'Type Job':<10}| {'xu-thêm':<7} | {'Số dư':<8} |"
)
JOB_TABLE_SEP = (
    f"|{'-' * 5}|{'-' * 19}|{'-' * 19}|{'-' * 11}|{'-' * 9}|{'-' * 10}|"
)


def print_job_table_header():
    print(f"\n{Fore.LIGHTWHITE_EX}{Style.BRIGHT}{JOB_TABLE_HEADER}{Style.RESET_ALL}")
    print(f"{Fore.LIGHTWHITE_EX}{Style.BRIGHT}{JOB_TABLE_SEP}{Style.RESET_ALL}")


def print_job_row(stt, uid, post_id, type_job, coin, balance, success):
    uid_text = str(uid or "-")[:17]
    post_text = str(post_id or "-")[:17]
    type_job_text = str(type_job or "Unknown")[:11]
    coin_text = f"+{format_coin(coin)}" if success else "0"
    balance_text = "-" if balance is None else format_coin(balance)
    color = Fore.LIGHTGREEN_EX if success else Fore.LIGHTRED_EX
    print(
        f"{color}| {stt:<3} | {uid_text:<17} | {post_text:<18}|"
        f"{type_job_text:<11}| {coin_text:<7} | {balance_text[:8]:<8} |"
        f"{Style.RESET_ALL}"
    )

def _facebook_job_link_for_display(job_link):
    if not isinstance(job_link, str) or not job_link.strip():
        return None
    job_link = job_link.strip()
    if job_link.lower().startswith(("http://", "https://")):
        return job_link
    return f"https://www.facebook.com/{job_link.lstrip('/')}"


def print_facebook_job_link(job_link):
    facebook_job_link = _facebook_job_link_for_display(job_link)
    if facebook_job_link:
        print(
            f"{Fore.BLUE}{Style.BRIGHT}\U0001f517 Link: "
            f"{facebook_job_link}{Style.RESET_ALL}"
        )


def print_ttc_server_response(job_type, server_raw_text):
    """Print TTC evidence while masking credential-shaped fields."""
    print_warning(f"[TTC][{job_type}]")
    print(_mask_sensitive_text(server_raw_text))


def print_ttc_exception(context, exc):
    """Keep typed TTC response evidence separate from an internal failure line."""
    server_raw_text = getattr(exc, "server_raw_text", "")
    if not isinstance(server_raw_text, str) or not server_raw_text:
        return False
    detail = str(exc).split("[TTC SERVER]", 1)[0].strip()
    detail = _mask_sensitive_text(detail)
    print_error(f"{context} {detail}".strip())
    if _runtime_debug_enabled():
        print_ttc_server_response(
            getattr(exc, "job_type", "") or "UNKNOWN", server_raw_text
        )
    return True


# ---------- Lưu / đọc cấu hình Termux/iSH ----------
# Code chỉ nằm trong file .py này. Dữ liệu runtime được lưu riêng trong 1 JSON để
# không tự sửa source khi chạy và dễ backup trên điện thoại.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_CONFIG_FILE = os.path.join(SCRIPT_DIR, "config_token.json")
DATA_FILE = os.environ.get("BUMX_TERMUX_DATA_FILE", os.path.join(SCRIPT_DIR, "bumx_termux_data.json"))
DATA_VERSION = 1
SESSION_PROFILE_ID = None


class ProfileInUseError(RuntimeError):
    pass


def runtime_lock_path(filename):
    data_dir = os.path.dirname(os.path.abspath(DATA_FILE))
    lock_dir = os.path.join(data_dir, ".bumx_runtime", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, filename)

@contextlib.contextmanager
def db_transaction_lock():
    lock_path = runtime_lock_path("database.lock")
    fd = None
    if fcntl is not None or msvcrt is not None:
        start_time = time.time()
        while True:
            try:
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if fd is not None:
                    os.close(fd)
                    fd = None
                if time.time() - start_time > 5.0:
                    raise RuntimeError("Dữ liệu đang được cập nhật bởi phiên khác. Vui lòng thử lại.")
                time.sleep(0.05)
    else:
        # Fallback for Windows/unsupported platforms during test
        print_warning("Hệ thống hiện tại không hỗ trợ khóa đa tiến trình an toàn.")
    try:
        yield
    finally:
        if fd is not None:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

def mutate_database(mutator):
    with db_transaction_lock():
        data = _load_database_unlocked()
        result = mutator(data)
        save_database(data)
        return result

@contextlib.contextmanager
def profile_run_lock(profile_id):
    if not profile_id:
        yield
        return
    lock_path = runtime_lock_path(f"profile_{profile_id}.run.lock")
    fd = None
    if fcntl is not None or msvcrt is not None:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            if fd is not None:
                os.close(fd)
            raise ProfileInUseError(f"Profile TTC {profile_id} đang được sử dụng ở phiên khác.")
    try:
        yield
    finally:
        if fd is not None:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)


class ProfileRunLease:
    """Long-lived owner for one profile run group.

    The existing ``profile_run_lock`` remains the cross-process primitive used
    by CLI and mutations.  A GUI acquires one lease for the whole group and
    its account workers deliberately do not reacquire the same exclusive lock.
    """

    def __init__(self, profile_id):
        self.profile_id = str(profile_id or "").strip()
        self._context = None
        self.held = False

    def acquire(self):
        if self.held:
            return self
        context = profile_run_lock(self.profile_id)
        context.__enter__()
        self._context = context
        self.held = True
        return self

    def release(self):
        if not self.held:
            return
        context = self._context
        context.__exit__(None, None, None)
        self._context = None
        self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False

DEFAULT_RUN_CONFIG = {
    "job_types": ["like_facebook", "like_poster"],
    "continuous": False,
    "max_jobs_per_acc": 30,
    "like_batch_min": 10,
    "like_batch_max": 10,
    "comment_batch_min": 5,
    "comment_batch_max": 5,
    "like_delay_min": 10,
    "like_delay_max": 10,
    "comment_delay_min": 10,
    "comment_delay_max": 10,
    "failed_delay_min": 10,
    "failed_delay_max": 10,
    "account_delay_min": 10,
    "account_delay_max": 10,
    "skip_hidden_comments": True,
    "selected_fb_account_keys": None,
}


def _atomic_json_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _normalize_account(account):
    if not isinstance(account, dict):
        raise ValueError("Facebook account phải là object")
    return {
        "email": str(account.get("email", "")).strip(),
        "password": str(account.get("password", "")),
        "secret": str(account.get("secret", "")),
        "token": str(account.get("token", "")).strip(),
    }


def normalize_run_config(data=None):
    config = dict(DEFAULT_RUN_CONFIG)
    if isinstance(data, dict):
        # Migrate các key cũ nếu người dùng đã từng thử bản Termux cũ.
        migrated = dict(data)
        if "like_batch_limit" in migrated:
            value = migrated.pop("like_batch_limit")
            migrated.setdefault("like_batch_min", value)
            migrated.setdefault("like_batch_max", value)
        if "comment_batch_limit" in migrated:
            value = migrated.pop("comment_batch_limit")
            migrated.setdefault("comment_batch_min", value)
            migrated.setdefault("comment_batch_max", value)
        if "normal_delay_min" in migrated or "normal_delay_max" in migrated:
            old_min = migrated.pop("normal_delay_min", 10)
            old_max = migrated.pop("normal_delay_max", old_min)
            for min_key, max_key in (
                ("like_delay_min", "like_delay_max"),
                ("comment_delay_min", "comment_delay_max"),
                ("account_delay_min", "account_delay_max"),
            ):
                migrated.setdefault(min_key, old_min)
                migrated.setdefault(max_key, old_max)
        for key in config:
            if key in migrated:
                config[key] = migrated[key]

    config["job_types"] = sorted(normalize_job_types(config["job_types"]))
    if not isinstance(config["continuous"], bool):
        config["continuous"] = bool(config["continuous"])
    if not isinstance(config["skip_hidden_comments"], bool):
        config["skip_hidden_comments"] = bool(config["skip_hidden_comments"])
    config["max_jobs_per_acc"] = validate_batch_limit(
        config["max_jobs_per_acc"], "max jobs per account"
    )
    resolve_burst_ranges(
        like_batch_min=config["like_batch_min"],
        like_batch_max=config["like_batch_max"],
        comment_batch_min=config["comment_batch_min"],
        comment_batch_max=config["comment_batch_max"],
    )
    resolve_run_delay_ranges(
        like_delay_min=config["like_delay_min"],
        like_delay_max=config["like_delay_max"],
        comment_delay_min=config["comment_delay_min"],
        comment_delay_max=config["comment_delay_max"],
        failed_delay_min=config["failed_delay_min"],
        failed_delay_max=config["failed_delay_max"],
        account_delay_min=config["account_delay_min"],
        account_delay_max=config["account_delay_max"],
    )
    selected = config.get("selected_fb_account_keys")
    if selected is not None:
        if not isinstance(selected, list) or any(not isinstance(key, str) or not key for key in selected):
            raise ValueError("selected_fb_account_keys không hợp lệ")
        config["selected_fb_account_keys"] = list(dict.fromkeys(selected))
    return config


_RUN_CONFIG_FACADE_JOB_TYPES = {
    "like": ("like_facebook",),
    "comment": ("like_poster",),
    "both": ("like_facebook", "like_poster"),
}
_RUN_CONFIG_FACADE_FIELDS = frozenset({
    "job_type",
    "max_jobs_per_acc",
    "job_delay_min",
    "job_delay_max",
    "account_delay_min",
    "account_delay_max",
    "continuous",
})


def get_run_config_facade(run_config=None):
    """Return the user-facing view; ``None`` delays mean existing mixed ranges."""
    config = normalize_run_config(run_config)
    enabled_types = frozenset(config["job_types"])
    job_type = next(
        (
            name for name, mapped_types in _RUN_CONFIG_FACADE_JOB_TYPES.items()
            if enabled_types == frozenset(mapped_types)
        ),
        None,
    )
    if job_type is None:
        raise ValueError("run config has no supported user-facing job type")

    like_delay = (config["like_delay_min"], config["like_delay_max"])
    comment_delay = (config["comment_delay_min"], config["comment_delay_max"])
    shared_delay = like_delay if like_delay == comment_delay else (None, None)

    return {
        "job_type": job_type,
        "max_jobs_per_acc": config["max_jobs_per_acc"],
        "job_delay_min": shared_delay[0],
        "job_delay_max": shared_delay[1],
        "account_delay_min": config["account_delay_min"],
        "account_delay_max": config["account_delay_max"],
        "continuous": config["continuous"],
    }


def apply_run_config_facade(run_config, facade_values):
    """Apply user-facing values while preserving all other run-config fields."""
    if not isinstance(facade_values, dict):
        raise ValueError("simplified run config must be an object")
    unknown_fields = set(facade_values) - _RUN_CONFIG_FACADE_FIELDS
    if unknown_fields:
        raise ValueError(
            f"Unsupported simplified run-config fields: {sorted(unknown_fields)}"
        )

    config = normalize_run_config(run_config)
    if "job_type" in facade_values:
        job_type = facade_values["job_type"]
        try:
            config["job_types"] = list(_RUN_CONFIG_FACADE_JOB_TYPES[job_type])
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "job_type must be one of: like, comment, both"
            ) from exc

    direct_fields = (
        "max_jobs_per_acc",
        "account_delay_min",
        "account_delay_max",
        "continuous",
    )
    for field in direct_fields:
        if field in facade_values:
            config[field] = facade_values[field]

    job_delay_fields = ("job_delay_min", "job_delay_max")
    job_delay_is_mixed = (
        all(field in facade_values for field in job_delay_fields)
        and all(facade_values[field] is None for field in job_delay_fields)
    )
    if not job_delay_is_mixed:
        if "job_delay_min" in facade_values:
            config["like_delay_min"] = facade_values["job_delay_min"]
            config["comment_delay_min"] = facade_values["job_delay_min"]
        if "job_delay_max" in facade_values:
            config["like_delay_max"] = facade_values["job_delay_max"]
            config["comment_delay_max"] = facade_values["job_delay_max"]

    return normalize_run_config(config)


def run_config_facade_summary(run_config):
    """Format only values exposed by the simplified run-config facade."""
    facade = get_run_config_facade(run_config)
    job_labels = {
        "like": "Like",
        "comment": "Comment",
        "both": "Like+Comment",
    }
    if facade["job_delay_min"] is None:
        delay_summary = "mixed"
    else:
        delay_summary = (
            f"{facade['job_delay_min']}-{facade['job_delay_max']}s"
        )
    return (
        f"Job={job_labels[facade['job_type']]} | "
        f"max={facade['max_jobs_per_acc']} | "
        f"delay={delay_summary} | "
        f"acc={facade['account_delay_min']}-{facade['account_delay_max']}s | "
        f"continuous={'ON' if facade['continuous'] else 'OFF'}"
    )


def _new_profile(name, auth_token="", fb_accounts=None):
    return {
        "profile_id": uuid.uuid4().hex,
        "name": str(name).strip() or "TTC",
        "auth_token": str(auth_token or "").strip(),
        "fb_accounts": [_normalize_account(item) for item in (fb_accounts or [])],
        "run_config": normalize_run_config(),
    }


def _empty_database():
    return {"version": DATA_VERSION, "active_profile_id": None, "profiles": []}


def _validate_database(data):
    if not isinstance(data, dict) or data.get("version") != DATA_VERSION:
        raise ValueError("File dữ liệu Termux không đúng version")
    if not isinstance(data.get("profiles"), list):
        raise ValueError("Danh sách TTC profile không hợp lệ")
    normalized_profiles = []
    ids = set()
    for raw in data["profiles"]:
        if not isinstance(raw, dict):
            raise ValueError("TTC profile không hợp lệ")
        profile_id = str(raw.get("profile_id", "")).strip()
        if not profile_id or profile_id in ids:
            raise ValueError("profile_id không hợp lệ hoặc bị trùng")
        ids.add(profile_id)
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("Tên TTC profile không được trống")
        normalized_profiles.append({
            "profile_id": profile_id,
            "name": name,
            "auth_token": str(raw.get("auth_token", "") or "").strip(),
            "fb_accounts": [_normalize_account(item) for item in raw.get("fb_accounts", [])],
            "run_config": normalize_run_config(raw.get("run_config")),
        })
    active = data.get("active_profile_id")
    if active not in ids:
        active = normalized_profiles[0]["profile_id"] if normalized_profiles else None
    return {"version": DATA_VERSION, "active_profile_id": active, "profiles": normalized_profiles}


def _read_legacy_config():
    if not os.path.isfile(LEGACY_CONFIG_FILE):
        return None
    try:
        with open(LEGACY_CONFIG_FILE, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            return None
        auth = str(data.get("auth_token") or "").strip()
        accounts = data.get("fb_accounts")
        if not isinstance(accounts, list):
            accounts = []
        if not auth and not accounts:
            return None
        return auth, accounts
    except (OSError, ValueError, TypeError):
        return None


def _load_database_unlocked():
    if os.path.isfile(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as stream:
            data = _validate_database(json.load(stream))
    else:
        data = _empty_database()
        legacy = _read_legacy_config()
        if legacy is not None:
            auth, accounts = legacy
            profile = _new_profile("TTC mặc định", auth, accounts)
            data["profiles"].append(profile)
            data["active_profile_id"] = profile["profile_id"]
    return data


def load_database():
    if os.path.isfile(DATA_FILE):
        return _load_database_unlocked()
    # First-time initialization is a real mutation and must use the DB transaction lock.
    return mutate_database(lambda data: data)


def save_database(data):
    normalized = _validate_database(data)
    _atomic_json_write(DATA_FILE, normalized)
    return normalized


def get_profile(data, profile_id=None):
    target = profile_id or SESSION_PROFILE_ID or data.get("active_profile_id")
    for profile in data.get("profiles", []):
        if profile.get("profile_id") == target:
            return profile
    return None


def save_profile(profile):
    """
    WARNING: Do not use for interactive stale read-modify-write flows.
    This replaces the entire profile object, which could overwrite fields
    changed by another process. Prefer targeted mutators instead.
    """
    def _mutator(data):
        profile_id = profile.get("profile_id")
        for index, existing in enumerate(data["profiles"]):
            if existing.get("profile_id") == profile_id:
                data["profiles"][index] = profile
                return profile
        raise KeyError("Không tìm thấy TTC profile")
    return mutate_database(_mutator)


def update_profile_auth_token(profile_id, token):
    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            return False
        profile["auth_token"] = str(token or "").strip()
        return True
    try:
        return mutate_database(_mutator)
    except Exception as exc:
        print_error(f"Token TTC đã refresh trong phiên hiện tại nhưng không thể lưu dữ liệu do xung đột/IO: {exc}")
        return False


def rename_profile(profile_id, new_name):
    new_name = str(new_name or "").strip()
    if not new_name:
        raise ValueError("Tên không được trống.")

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy TTC profile")
        profile["name"] = new_name
        return profile["name"]

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def update_profile_auth_token_manual(profile_id, token):
    token = str(token or "").strip()

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy TTC profile")
        profile["auth_token"] = token
        return True

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def delete_profile(profile_id):
    def _mutator(data):
        for index, profile in enumerate(data["profiles"]):
            if profile.get("profile_id") == profile_id:
                deleted = data["profiles"].pop(index)
                if data.get("active_profile_id") == profile_id:
                    data["active_profile_id"] = (
                        data["profiles"][0]["profile_id"] if data["profiles"] else None
                    )
                return deleted, data.get("active_profile_id")
        raise KeyError("Không tìm thấy TTC profile")

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def select_profile_as_default(profile_id):
    def _mutator(data):
        if get_profile(data, profile_id) is None:
            raise KeyError("Không tìm thấy TTC profile")
        data["active_profile_id"] = profile_id
        return True

    return mutate_database(_mutator)


def create_profile_record(name, token):
    profile = _new_profile(name, token)

    def _mutator(data):
        data['profiles'].append(profile)
        data['active_profile_id'] = profile['profile_id']
        return profile

    return mutate_database(_mutator)


def load_config():
    """Compatibility helper: trả config của TTC profile đang chọn."""
    data = load_database()
    profile = get_profile(data)
    if profile is None:
        return {"auth_token": None, "fb_accounts": []}
    return {"auth_token": profile.get("auth_token") or None, "fb_accounts": list(profile["fb_accounts"])}


def save_config(auth_token, fb_accounts):
    """Compatibility helper: chỉ lưu vào TTC profile đang chọn."""
    def _mutator(data):
        profile = get_profile(data)
        if profile is None:
            profile = _new_profile("TTC mặc định", auth_token, fb_accounts)
            data["profiles"].append(profile)
            data["active_profile_id"] = profile["profile_id"]
        else:
            profile["auth_token"] = str(auth_token or "").strip()
            profile["fb_accounts"] = [_normalize_account(acc) for acc in (fb_accounts or [])]
    mutate_database(_mutator)


def secret_input(prompt):
    try:
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt)


def preview_token(token):
    value = str(token or "")
    if not value:
        return "(trống)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def facebook_account_key(account):
    email = str(account.get("email", "")).strip().casefold()
    if email:
        return "email:" + email
    token = str(account.get("token", "")).strip()
    if token:
        return "token_sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    return ""


def selected_account_keys(profile):
    accounts = profile.get("fb_accounts", [])
    explicit = profile.get("run_config", {}).get("selected_fb_account_keys")
    all_keys = [facebook_account_key(account) for account in accounts]
    all_keys = [key for key in all_keys if key]
    if explicit is None:
        return tuple(all_keys)
    allowed = set(all_keys)
    return tuple(key for key in explicit if key in allowed)


def selected_accounts(profile):
    selected = set(selected_account_keys(profile))
    return [
        account for account in profile.get("fb_accounts", [])
        if facebook_account_key(account) in selected and str(account.get("token", "")).strip()
    ]


def get_profile_snapshot(profile_id):
    """Load the latest validated profile object for a non-interactive caller."""
    profile = get_profile(load_database(), profile_id)
    if profile is None:
        raise KeyError("Không tìm thấy TTC profile")
    return profile


def add_manual_account_record(profile_id, token):
    """Add a manual-token account without CLI prompts."""
    token = str(token or "").strip()
    if not token:
        raise ValueError("Token Facebook không được để trống.")
    account = {"email": "", "password": "", "secret": "", "token": token}

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy TTC profile")
        accounts = profile.setdefault("fb_accounts", [])
        if _is_duplicate_account(accounts, account):
            raise ValueError("Facebook account/token đã tồn tại trong TTC profile này.")
        accounts.append(dict(account))
        _include_new_account_in_explicit_selection(profile, account)
        return dict(account)

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def update_account_record(profile_id, account_key, updates, enabled=None):
    """Update only named fields on the latest account record."""
    if not isinstance(updates, dict):
        raise ValueError("Dữ liệu cập nhật Facebook account không hợp lệ.")
    allowed_fields = {"email", "password", "secret", "token"}
    unknown_fields = set(updates) - allowed_fields
    if unknown_fields:
        raise ValueError(f"Trường Facebook account không hỗ trợ: {sorted(unknown_fields)}")
    normalized_updates = {
        field: (str(value or "").strip() if field in {"email", "token"} else str(value or ""))
        for field, value in updates.items()
    }

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy TTC profile")
        accounts = profile.get("fb_accounts", [])
        target_index = next(
            (index for index, account in enumerate(accounts)
             if facebook_account_key(account) == account_key),
            None,
        )
        if target_index is None:
            raise ValueError("Facebook account không còn tồn tại trong dữ liệu mới nhất.")
        updated = dict(accounts[target_index])
        updated.update(normalized_updates)
        updated = _normalize_account(updated)
        if not updated["token"]:
            raise ValueError("Facebook token không được để trống.")
        if _is_duplicate_account(accounts, updated, ignore_index=target_index):
            raise ValueError("Email hoặc token mới bị trùng với account khác.")

        accounts[target_index] = updated
        new_key = facebook_account_key(updated)
        selected = profile["run_config"].get("selected_fb_account_keys")
        if selected is not None and new_key != account_key:
            was_selected = account_key in selected
            selected[:] = [key for key in selected if key != account_key]
            if was_selected and new_key and new_key not in selected:
                selected.append(new_key)
        if enabled is not None:
            if selected is None:
                selected = [facebook_account_key(account) for account in accounts]
                selected = [key for key in selected if key]
                profile["run_config"]["selected_fb_account_keys"] = selected
            selected[:] = [key for key in selected if key != new_key]
            if bool(enabled) and new_key:
                selected.append(new_key)
        return {"account": dict(updated), "account_key": new_key}

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def delete_account_record(profile_id, account_key):
    """Delete one latest account record by its stable core key."""
    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy TTC profile")
        accounts = profile.get("fb_accounts", [])
        target_index = next(
            (index for index, account in enumerate(accounts)
             if facebook_account_key(account) == account_key),
            None,
        )
        if target_index is None:
            raise ValueError("Facebook account không còn tồn tại trong dữ liệu mới nhất.")
        deleted = accounts.pop(target_index)
        selected = profile["run_config"].get("selected_fb_account_keys")
        if selected is not None:
            selected[:] = [key for key in selected if key != account_key]
        return deleted

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def relogin_account_record(profile_id, account_key, password=None, secret=None,
                           req_fb=None):
    """Login outside locks, then atomically replace credentials/token."""
    snapshot = get_profile_snapshot(profile_id)
    account = next(
        (item for item in snapshot.get("fb_accounts", [])
         if facebook_account_key(item) == account_key),
        None,
    )
    if account is None:
        raise ValueError("Facebook account không còn tồn tại trong dữ liệu mới nhất.")
    email = str(account.get("email", "")).strip()
    if not email:
        raise ValueError("Manual-token account không có UID/email để login lại.")
    login_password = str(account.get("password", "")) if password is None else str(password)
    login_secret = str(account.get("secret", "")) if secret is None else str(secret)
    if not login_password:
        raise ValueError("Password không được để trống khi login lại.")

    owns_client = req_fb is None
    login_client = Requests_facebook() if owns_client else req_fb
    try:
        token = login_client.login_and_get_token(email, login_password, login_secret)
    finally:
        if owns_client:
            login_client.close()
    if not token:
        raise RuntimeError("Login lại thất bại; token cũ vẫn được giữ.")
    updates = {"token": token}
    if password is not None:
        updates["password"] = login_password
    if secret is not None:
        updates["secret"] = login_secret
    return update_account_record(profile_id, account_key, updates)


def update_profile_run_config(profile_id, updates):
    """Merge validated run-config fields into the latest profile snapshot."""
    if not isinstance(updates, dict):
        raise ValueError("Cấu hình chạy không hợp lệ.")
    unknown_fields = set(updates) - set(DEFAULT_RUN_CONFIG)
    if unknown_fields:
        raise ValueError(f"Trường cấu hình không hỗ trợ: {sorted(unknown_fields)}")

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy TTC profile")
        latest = normalize_run_config(profile.get("run_config"))
        latest.update(updates)
        normalized = normalize_run_config(latest)
        profile["run_config"] = normalized
        return dict(normalized)

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def copy_facebook_account_records(source_profile_id, destination_profile_id,
                                  account_keys=None):
    """Copy selected latest accounts between profiles without stale writes."""
    requested_keys = None if account_keys is None else list(dict.fromkeys(account_keys))

    def _mutator(data):
        source = get_profile(data, source_profile_id)
        destination = get_profile(data, destination_profile_id)
        if source is None or destination is None:
            raise KeyError("Không tìm thấy profile nguồn hoặc đích")
        source_by_key = {
            facebook_account_key(account): account
            for account in source.get("fb_accounts", [])
            if facebook_account_key(account)
        }
        keys = list(source_by_key) if requested_keys is None else requested_keys
        if any(key not in source_by_key for key in keys):
            raise ValueError("Facebook account nguồn đã thay đổi. Vui lòng chọn lại.")
        copied = 0
        skipped = 0
        destination_accounts = destination.setdefault("fb_accounts", [])
        for key in keys:
            candidate = dict(source_by_key[key])
            if _is_duplicate_account(destination_accounts, candidate):
                skipped += 1
                continue
            destination_accounts.append(candidate)
            _include_new_account_in_explicit_selection(destination, candidate)
            copied += 1
        return {"copied": copied, "skipped": skipped}

    with profile_run_lock(destination_profile_id):
        return mutate_database(_mutator)

# ---------- Class Facebook login (từ file cũ) ----------
_FACEBOOK_LOGIN_MESSAGES = {
    "SUCCESS": "Đăng nhập Facebook thành công.",
    "2FA_REQUIRED": "Facebook yêu cầu 2FA.",
    "CHECKPOINT": "Facebook yêu cầu checkpoint/xác minh tài khoản.",
    "INVALID_CREDENTIALS": "UID/email hoặc mật khẩu không được Facebook chấp nhận.",
    "LOGIN_REJECTED": "Facebook từ chối đăng nhập.",
    "UNKNOWN_RESPONSE": "Facebook trả về trạng thái đăng nhập chưa được parser hỗ trợ.",
}

_FACEBOOK_DIAGNOSTIC_MARKERS = (
    ("access_token", ("access_token",)),
    ("two_step_verification_context", ("two_step_verification_context",)),
    ("checkpoint", ("checkpoint",)),
    ("challenge", ("challenge",)),
    ("two_factor", ("two_factor", "two_step")),
    ("approval", ("login_approval", "login approval", "approval")),
    (
        "verification",
        ("verification", "confirm_identity", "confirm identity", "security"),
    ),
    ("error", ("error",)),
    ("password", ("password",)),
    ("invalid", ("invalid", "incorrect")),
    ("blocked", ("blocked",)),
    ("disabled", ("disabled",)),
    ("suspended", ("suspended",)),
)


def _facebook_response_text(response):
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    if isinstance(response, (dict, list)):
        try:
            return json.dumps(response, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return str(getattr(response, "text", "") or "")


def _collect_facebook_json_keys(value, keys, max_keys=40, depth=0):
    if depth > 20 or len(keys) >= max_keys:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if len(keys) >= max_keys:
                return
            if isinstance(key, str):
                keys.add(key)
            _collect_facebook_json_keys(nested, keys, max_keys, depth + 1)
    elif isinstance(value, list):
        for nested in value:
            if len(keys) >= max_keys:
                return
            _collect_facebook_json_keys(nested, keys, max_keys, depth + 1)


def _safe_facebook_header(response, header_name):
    headers = getattr(response, "headers", None)
    if not headers:
        return ""
    value = headers.get(header_name)
    if value is None:
        target = header_name.casefold()
        for key, candidate in headers.items():
            if str(key).casefold() == target:
                value = candidate
                break
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")[:160]


def build_facebook_login_diagnostic(response, stage, classifier_status):
    response_text = _facebook_response_text(response)
    searchable = response_text.casefold()
    json_keys = set()
    try:
        parsed = json.loads(response_text)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        parsed = None
    if parsed is not None:
        _collect_facebook_json_keys(parsed, json_keys)

    return {
        "stage": str(stage),
        "http_status": getattr(response, "status_code", None),
        "content_type": _safe_facebook_header(response, "Content-Type"),
        "content_encoding": _safe_facebook_header(response, "Content-Encoding"),
        "response_length": len(response_text),
        "markers": {
            name: any(marker in searchable for marker in aliases)
            for name, aliases in _FACEBOOK_DIAGNOSTIC_MARKERS
        },
        "known_json_keys": sorted(json_keys)[:40],
        "classifier_result": str(classifier_status),
    }


def print_facebook_login_diagnostic(response, stage, classifier_status):
    if classifier_status != "UNKNOWN_RESPONSE":
        return False

    diagnostic = build_facebook_login_diagnostic(
        response, stage, classifier_status
    )
    print("\n=== FACEBOOK LOGIN DIAGNOSTIC ===")
    print(f"Stage: {diagnostic['stage']}")
    print(f"HTTP: {diagnostic['http_status']}")
    print(f"Content-Type: {diagnostic['content_type']}")
    if diagnostic["content_encoding"]:
        print(f"Content-Encoding: {diagnostic['content_encoding']}")
    print(f"Length: {diagnostic['response_length']}")
    print()
    for marker_name, _aliases in _FACEBOOK_DIAGNOSTIC_MARKERS:
        marker_value = "YES" if diagnostic["markers"][marker_name] else "NO"
        print(f"Contains {marker_name}: {marker_value}")
    print()
    print(
        "Known JSON keys: "
        + json.dumps(diagnostic["known_json_keys"], ensure_ascii=False)
    )
    print()
    print("Classifier result:")
    print(diagnostic["classifier_result"])
    print("\n===============================")
    return True


def _response_text_candidates(response):
    if response is None:
        text = ""
    elif isinstance(response, str):
        text = response
    elif isinstance(response, bytes):
        text = response.decode("utf-8", errors="replace")
    elif isinstance(response, (dict, list)):
        try:
            text = json.dumps(response, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(response)
    else:
        text = str(response)

    candidates = []
    current = text
    for _ in range(8):
        if current not in candidates:
            candidates.append(current)
        reduced = current.replace("\\\\", "\\").replace('\\"', '"')
        if reduced == current:
            break
        current = reduced
    return tuple(candidates)


def _find_structured_field(value, field_name, depth=0):
    if depth > 12:
        return None
    if isinstance(value, dict):
        direct = value.get(field_name)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for nested in value.values():
            found = _find_structured_field(nested, field_name, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, list):
        for nested in value:
            found = _find_structured_field(nested, field_name, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, str) and value.strip():
        try:
            nested = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        if nested == value:
            return None
        return _find_structured_field(nested, field_name, depth + 1)
    return None


def _extract_facebook_field(response, field_name):
    if isinstance(response, (dict, list)):
        structured = _find_structured_field(response, field_name)
        if structured:
            return structured

    for candidate in _response_text_candidates(response):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if parsed is not None:
            structured = _find_structured_field(parsed, field_name)
            if structured:
                return structured

        match = re.search(
            r'"' + re.escape(field_name) + r'"\s*:\s*"([^"\\\s]{1,4096})"',
            candidate,
        )
        if match:
            return match.group(1)
    return None


def _extract_access_token(response_text):
    return _extract_facebook_field(response_text, "access_token")


def _extract_two_factor_context(response_text):
    context = _extract_facebook_field(
        response_text, "two_step_verification_context"
    )
    if context:
        return context

    legacy_pattern = re.compile(
        r'\(dkc\s+"two_step_verification_context"[^()\r\n]{0,1024}\)'
        r'\s*\(dkc\s+"([^"\\\s]{1,4096})"'
    )
    for candidate in _response_text_candidates(response_text):
        match = legacy_pattern.search(candidate)
        if match:
            return match.group(1)
    return None


def _parse_facebook_login_response(response):
    token = _extract_access_token(response)
    if token:
        return {
            "status": "SUCCESS",
            "token": token,
            "two_factor_context": None,
            "message": _FACEBOOK_LOGIN_MESSAGES["SUCCESS"],
        }

    two_factor_context = _extract_two_factor_context(response)
    if two_factor_context:
        return {
            "status": "2FA_REQUIRED",
            "token": None,
            "two_factor_context": two_factor_context,
            "message": _FACEBOOK_LOGIN_MESSAGES["2FA_REQUIRED"],
        }

    searchable = "\n".join(_response_text_candidates(response)).casefold()
    checkpoint_markers = (
        "checkpoint",
        "account verification",
        "account_verification",
        "login approval",
        "login_approval",
        "confirm identity",
        "confirm_identity",
        "security check",
        "security_check",
        "challenge_required",
    )
    invalid_credential_markers = (
        "invalid_credentials",
        "invalid credentials",
        "incorrect password",
        "invalid password",
        "wrong password",
        "invalid username",
        "unrecognized email",
        "credentials rejected",
    )
    rejected_markers = (
        "login_rejected",
        "login rejected",
        "login_denied",
        "login denied",
        "authentication failed",
        "unable to log in",
        "login failed",
    )

    if any(marker in searchable for marker in checkpoint_markers):
        status = "CHECKPOINT"
    elif any(marker in searchable for marker in invalid_credential_markers):
        status = "INVALID_CREDENTIALS"
    elif any(marker in searchable for marker in rejected_markers):
        status = "LOGIN_REJECTED"
    else:
        status = "UNKNOWN_RESPONSE"
    return {
        "status": status,
        "token": None,
        "two_factor_context": None,
        "message": _FACEBOOK_LOGIN_MESSAGES[status],
    }


class Requests_facebook:
    def __init__(self):
        self._requests = requests.Session()
        self._requests.headers.update({
            "User-Agent": "[FBAN/FB4A;FBAV/571.0.0.44.73;FBBV/1023572511;FBDM/{density=1.5,width=540,height=960};FBLC/en_US;FBRV/0;FBCR/Verizon;FBMF/samsung;FBBD/samsung;FBPN/com.facebook.katana;FBDV/SM-S901N;FBSV/9;FBOP/1;FBCA/x86_64:arm64-v8a;]",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Priority": "u=0",
            "app-scope-id-header": "77a256c1-5911-48a4-bafd-fa4316c9f941",
            "x-fb-client-ip": "True",
            "x-fb-conn-uuid-client": "AS4k/LynmUQ6y62aIVsNWw==",
            "X-FB-Connection-Type": "WIFI",
            "x-fb-device-group": "1904",
            "X-FB-HTTP-Engine": "Tigon/Liger",
            "X-FB-Net-HNI": "310005",
            "X-FB-Request-Analytics-Tags": '{"network_tags":{"product":"350685531728","request_category":"graphql","purpose":"fetch","retry_attempt":"0"},"application_tags":"graphservice"}',
            "x-fb-rmd": "fail=Server:INVALID_MAP,Default:INVALID_MAP;v=;ip=;tkn=;reqTime=1633641062;recvTime=0",
            "x-fb-server-cluster": "True",
            "X-FB-SIM-HNI": "310005",
            "x-graphql-client-library": "graphservice",
            "x-graphql-request-purpose": "fetch",
            "X-Meta-USDID": "28266e60-2c0c-48f2-b64d-d25fee3d714e.1785300592.MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAELj5WOTDR3Uvqc5tqJ5lWdL_Mdi5xxeE342NEI82WLzzDIBKX4Q6wQ7hwtZDInM4tf_pkkNKKsoBQo_2cQQbIvQ.MEUCIQCx1YdU9dWWQRQ-bxEA83dBGnCWy5pAm3ed5KQUnoQlSAIgX_ISrCeup82fNh7OBMl-jZQ18eNbmooTjFzt6Ne12XY",
            "X-Meta-ZCA": "empty_token",
            "x-tigon-is-retry": "False",
            "x-zero-eh": "664c0faaac849cb891d0a261fbb72a12",
            "X-ZERO-STATE": "unknown",

        })
        self._url_api = 'https://b-graph.facebook.com/graphql'
        self._last_login_result = None

    def _record_login_result(self, result, stage, response=None):
        recorded = dict(result)
        recorded["stage"] = stage
        if response is not None:
            recorded["http_status"] = getattr(response, "status_code", None)
            response_text = _facebook_response_text(response)
            recorded["response_length"] = len(response_text)
        self._last_login_result = recorded
        if response is not None and recorded.get("status") == "UNKNOWN_RESPONSE":
            print_facebook_login_diagnostic(response, stage, recorded["status"])
        return recorded

    def _login_facebook(self, uid, password, encode_2fa):
        # Hàm này giữ nguyên từ file cũ
        import time
        import uuid
        import base64
        import json
        import pyotp

        ts = int(time.time())
        device = str(uuid.uuid4())
        client_trace_id = str(uuid.uuid4())
        waterfall_id = str(uuid.uuid4())

        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "CreateOneTimeChallengeNonceCreate",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '14372771303129768114139595837',
            'fb_api_client_context': '{"is_background":false}',
            'variables': r'''{"input":{"app_scoped_device_id":"%s"}}''' % (device),
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }

        res = self._requests.post(url=self._url_api, headers={
            "Authorization": "OAuth 350685531728|62f8ce9f74b12f84c123cc23437a4a32",
            "X-FB-Friendly-Name": "CreateOneTimeChallengeNonceCreate",
            "X-ZERO-F-DEVICE-ID": device,
        }, data=payload, timeout=FB_LOGIN_TIMEOUT)

        data = res.json()
        print(data)

        challenge_nonce = None
        if isinstance(data, dict):
            nonce_container = data.get("data")
            if isinstance(nonce_container, dict):
                nonce_result = nonce_container.get(
                    "xfb_fb_key_attestation_create_one_time_challenge_nonce"
                )
                if isinstance(nonce_result, dict):
                    challenge_nonce = nonce_result.get("challenge_nonce")
        if not isinstance(challenge_nonce, str) or not challenge_nonce.strip():
            self._record_login_result({
                "status": "MALFORMED_RESPONSE",
                "token": None,
                "two_factor_context": None,
                "message": "Facebook không trả challenge_nonce hợp lệ.",
            }, "CHALLENGE_NONCE", res)
            return None

        data = {
            "challenge_nonce": challenge_nonce,
            "username": uid
        }
        data_challenge_nonce = base64.b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode()
        client_trace_id = str(uuid.uuid4())

        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "FbBloksActionRootQuery-com.bloks.www.bloks.caa.login.async.send_login_request",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '119940804212792425089909966247',
            'fb_api_client_context': '{"is_background":false}',
            'variables': r'''{"params":{"params":"{\"params\":\"{\\\"server_params\\\":{\\\"device_id\\\":\\\"%s\\\",\\\"server_login_source\\\":\\\"login\\\",\\\"waterfall_id\\\":\\\"%s\\\",\\\"attestation_result\\\":{\\\"keyHash\\\":\\\"c816e3d25740b8692da6c3e168baf6d38c3e3b855b10f114a6900aa70e386bf2\\\",\\\"signature\\\":\\\"MEUCIQDhOJq8dMQu5S1SWRENyPxV6kMSmaXCAXGuLQnF\\/6wdCAIgBr\\/suDJIvrhXd\\/iWyNlCQUv1pUd8WoEW5pawWtqlpIA=\\\",\\\"data\\\":\\\"%s\\\"},\\\"machine_id\\\":\\\"p4Rpar0qseBOPtkrB_exw55d\\\",\\\"from_native_screen\\\":true,\\\"credential_type\\\":\\\"password\\\",\\\"password\\\":\\\"#PWD_FB4A:0:%s:%s\\\",\\\"try_num\\\":\\\"1\\\",\\\"family_device_id\\\":\\\"%s\\\",\\\"event_flow\\\":\\\"login_manual\\\",\\\"event_step\\\":\\\"home_page\\\",\\\"is_from_logged_in_switcher\\\":false,\\\"contact_point\\\":\\\"%s\\\"}}\"}","bloks_versioning_id":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f","app_id":"com.bloks.www.bloks.caa.login.async.send_login_request"},"scale":"1.5","nt_context":{"using_white_navbar":true,"styles_id":"a07b73c926a84224348806e6cd486365","pixel_ratio":1.5,"is_push_on":true,"debug_tooling_metadata_token":null,"is_flipper_enabled":false,"theme_params":[{"value":["three_neutral_gray"],"design_system_name":"XMDS"},{"value":[],"design_system_name":"FDS"}],"bloks_version":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"}}''' % (device, waterfall_id, data_challenge_nonce, ts, password, device, str(uid)),
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }

        res = self._requests.post(url=self._url_api, headers={
            "Authorization": "OAuth 350685531728|62f8ce9f74b12f84c123cc23437a4a32",
            "X-FB-Friendly-Name": "FbBloksActionRootQuery-com.bloks.www.bloks.caa.login.async.send_login_request"
        }, data=payload, timeout=FB_LOGIN_TIMEOUT)

        data = res.text
        password_result = _parse_facebook_login_response(data)
        self._record_login_result(password_result, "PASSWORD_LOGIN", res)
        if password_result["status"] == "SUCCESS":
            return password_result["token"]
        if password_result["status"] != "2FA_REQUIRED":
            return None
        if not str(encode_2fa or "").strip():
            password_result["message"] = (
                "Facebook yêu cầu 2FA nhưng account chưa có 2FA secret."
            )
            self._record_login_result(password_result, "PASSWORD_LOGIN", res)
            return None

        two_step_verification_context = password_result["two_factor_context"]
        try:
            secret = str(encode_2fa).replace(" ", "")
            totp = pyotp.TOTP(secret)

            payload = {
                "method": "post",
                "format": "json",
                "server_timestamps": "true",
                "locale": "en_US",
                "purpose": "fetch",
                "fb_api_req_friendly_name": "FbBloksActionRootQuery-com.bloks.www.two_step_verification.verify_code.async",
                'fb_api_caller_class': 'graphservice',
                'client_doc_id': '119940804212792425089909966247',
                'fb_api_client_context': '{"is_background":false}',
                'variables': r'''{"params":{"params":"{\"params\":\"{\\\"client_input_params\\\":{\\\"auth_secure_device_id\\\":\\\"\\\",\\\"block_store_machine_id\\\":null,\\\"code\\\":\\\"%s\\\",\\\"should_trust_device\\\":1,\\\"family_device_id\\\":\\\"77a256c1-5911-48a4-bafd-fa4316c9f941\\\",\\\"device_id\\\":\\\"77a256c1-5911-48a4-bafd-fa4316c9f941\\\",\\\"cloud_trust_token\\\":null,\\\"network_bssid\\\":null,\\\"machine_id\\\":\\\"\\\"},\\\"server_params\\\":{\\\"INTERNAL__latency_qpl_marker_id\\\":36707139,\\\"device_id\\\":null,\\\"spectra_reg_login_data\\\":null,\\\"challenge\\\":\\\"totp\\\",\\\"INTERNAL__latency_qpl_instance_id\\\":130518416400274,\\\"two_step_verification_context\\\":\\\"%s\\\",\\\"flow_source\\\":\\\"two_factor_login\\\"}}\"}","bloks_versioning_id":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f","app_id":"com.bloks.www.two_step_verification.verify_code.async"},"scale":"1.5","nt_context":{"using_white_navbar":true,"styles_id":"a07b73c926a84224348806e6cd486365","pixel_ratio":1.5,"is_push_on":true,"debug_tooling_metadata_token":null,"is_flipper_enabled":false,"theme_params":[{"value":["three_neutral_gray"],"design_system_name":"XMDS"},{"value":[],"design_system_name":"FDS"}],"bloks_version":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"}}''' % (totp.now(), two_step_verification_context),
                'fb_api_analytics_tags': '["GraphServices"]',
                'client_trace_id': '73b659d1-a3cc-4db4-bc79-892b41b88f2c'
            }
            res = self._requests.post(url=self._url_api, headers={
                "Authorization": "OAuth 350685531728|62f8ce9f74b12f84c123cc23437a4a32",
                "X-FB-Friendly-Name": "FbBloksActionRootQuery-com.bloks.www.two_step_verification.verify_code.async"
            }, data=payload, timeout=FB_LOGIN_TIMEOUT)
            data = res.text
        except (binascii.Error, TypeError, ValueError):
            self._record_login_result({
                "status": "TWO_FACTOR_FAILED",
                "token": None,
                "two_factor_context": None,
                "message": "2FA secret không hợp lệ.",
            }, "TWO_FACTOR_VERIFY")
            return None

        two_factor_result = _parse_facebook_login_response(data)
        if two_factor_result["status"] == "2FA_REQUIRED":
            two_factor_result = dict(two_factor_result)
            two_factor_result["status"] = "TWO_FACTOR_FAILED"
            two_factor_result["message"] = "Facebook không chấp nhận mã 2FA."
        elif two_factor_result["status"] == "UNKNOWN_RESPONSE":
            two_factor_result = dict(two_factor_result)
            two_factor_result["message"] = (
                "Facebook trả về trạng thái xác minh 2FA chưa được parser hỗ trợ."
            )
        self._record_login_result(two_factor_result, "TWO_FACTOR_VERIFY", res)
        if two_factor_result["status"] == "SUCCESS":
            return two_factor_result["token"]
        return None

    def login_and_get_token(self, email, password, secret_2fa=""):
        """
        Đăng nhập Facebook và trả về access token.
        secret_2fa: chuỗi secret TOTP (nếu có)
        """
        try:
            token = self._login_facebook(email, password, secret_2fa)
        except Exception as exc:
            self._record_login_result({
                "status": "NETWORK_RESPONSE_ERROR",
                "token": None,
                "two_factor_context": None,
                "message": "Lỗi request/response Facebook không mong đợi.",
            }, "LOGIN_REQUEST")
            print_error(
                f"Login Facebook thất bại: {self._last_login_result['message']}"
            )
            return None
        if token:
            return token
        result = self._last_login_result or {
            "message": _FACEBOOK_LOGIN_MESSAGES["UNKNOWN_RESPONSE"]
        }
        print_error(f"Login Facebook thất bại: {result['message']}")
        return None

    def close(self):
        self._requests.close()

# ---------- Hàm tạo session/attribution (giữ nguyên) ----------
def generate_session_id(prefix="UFS", suffix="fg-4"):
    unique_id = uuid.uuid4()
    return f"{prefix}-{unique_id}-{suffix}"

def generate_fragment(name: str, source: str, duration: float = 0.0):
    now = time.time()
    return {
        'name': name,
        'source': source,
        'start_time': now,
        'end_time': now + duration,
        'instance_id': random.randint(100000, 999999999)
    }

def build_attribution(fragments):
    items = []
    for f in fragments:
        item = f"{f['name']},{f['source']},,{f['start_time']},{f['instance_id']},,,,{f['end_time']}"
        items.append(item)
    return ';'.join(items)

# ---------- Class AUTO_Action (giữ nguyên) ----------
def _decode_facebook_comment_parent_id(comment_id):
    """Return the parent post from the known encoded Facebook comment ID form."""
    if not isinstance(comment_id, str) or not comment_id:
        return None
    try:
        decoded = base64.b64decode(comment_id, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    match = re.fullmatch(r"comment:([^_\s]+)_([^_\s]+)", decoded)
    return match.group(1) if match else None


def _facebook_comment_response_evidence(comment_data):
    evidence = {
        "comment_id": None,
        "parent_post_id": None,
        "outgoing_text": None,
        "comment_keys": (),
        "returned_text": None,
        "returned_text_fields": (),
        "returned_actor_id": None,
        "returned_actor_fields": (),
        "feedback_id": None,
        "hidden": None,
        "pending": None,
        "visibility_fields": {},
        "visibility_scalars": {},
        "visibility_structures": {},
    }
    if not isinstance(comment_data, dict):
        return evidence

    evidence["comment_keys"] = tuple(sorted(str(key) for key in comment_data))
    comment_id = comment_data.get("id")
    if isinstance(comment_id, str) and comment_id:
        evidence["comment_id"] = comment_id
        evidence["parent_post_id"] = _decode_facebook_comment_parent_id(comment_id)

    text_fields = []
    for container_name in ("body", "message"):
        container = comment_data.get(container_name)
        if isinstance(container, dict) and isinstance(container.get("text"), str):
            text_fields.append((f"{container_name}.text", container["text"]))
    if isinstance(comment_data.get("text"), str):
        text_fields.append(("text", comment_data["text"]))
    evidence["returned_text_fields"] = tuple(text_fields)
    if text_fields:
        evidence["returned_text"] = text_fields[0][1]

    actor_fields = []
    for container_name in ("author", "actor"):
        container = comment_data.get(container_name)
        if isinstance(container, dict) and isinstance(container.get("id"), (str, int)):
            if not isinstance(container["id"], bool):
                actor_fields.append((f"{container_name}.id", str(container["id"])))
    evidence["returned_actor_fields"] = tuple(actor_fields)
    if actor_fields:
        evidence["returned_actor_id"] = actor_fields[0][1]

    feedback = comment_data.get("feedback")
    if isinstance(feedback, dict) and isinstance(feedback.get("id"), (str, int)):
        if not isinstance(feedback["id"], bool):
            evidence["feedback_id"] = str(feedback["id"])

    visibility_scalar_names = (
        "comment_privacy_value",
        "spam_display_mode",
        "is_viewer_comment_poster",
        "is_declined_by_group_admin_assistant",
        "can_viewer_delete",
        "can_viewer_edit",
        "created_time",
        "legacy_fbid",
        "url",
        "depth",
    )
    for key in visibility_scalar_names:
        value = comment_data.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            evidence["visibility_scalars"][key] = value

    for key, value in comment_data.items():
        if key in {"is_hidden", "is_pending"} or "visibility" in str(key).lower():
            if value is None or isinstance(value, (str, int, float, bool)):
                evidence["visibility_fields"][str(key)] = value

    for key in (
        "group_comment_info",
        "comment_tombstone_upsell_config",
        "private_reply_context",
        "real_author_for_moderation",
    ):
        value = comment_data.get(key)
        if not isinstance(value, dict):
            evidence["visibility_structures"][key] = {
                "type": "NOT AVAILABLE" if value is None else type(value).__name__,
                "keys": (),
                "safe_scalar_flags": {},
            }
            continue
        evidence["visibility_structures"][key] = {
            "type": "dict",
            "keys": tuple(sorted(str(item) for item in value)),
            "safe_scalar_flags": {
                str(item): flag
                for item, flag in value.items()
                if isinstance(flag, bool) or flag is None
            },
        }
    evidence["hidden"] = evidence["visibility_fields"].get("is_hidden")
    evidence["pending"] = evidence["visibility_fields"].get("is_pending")
    return evidence


def _is_comment_pending_approval(evidence):
    """Return true only for Facebook's explicit non-claimable comment state."""
    if not isinstance(evidence, dict):
        return False
    visibility = evidence.get("visibility_scalars")
    return (
        isinstance(visibility, dict)
        and visibility.get("comment_privacy_value") == "PENDING_APPROVAL"
    )


FACEBOOK_LIVE = "LIVE"
FACEBOOK_DIE_TOKEN = "DIE_TOKEN"
FACEBOOK_CHECK_ERROR = "CHECK_ERROR"


@dataclass(frozen=True)
class FacebookLiveCheckResult:
    status: str
    uid: str = None
    reason: str = ""
    http_status: int = None

    def to_dict(self):
        return {
            "status": self.status,
            "uid": self.uid,
            "reason": self.reason,
            "http_status": self.http_status,
        }


def _normalize_facebook_live_result(result):
    if isinstance(result, FacebookLiveCheckResult):
        status = result.status
        uid = result.uid
        reason = result.reason
        http_status = result.http_status
    elif isinstance(result, dict):
        status = result.get("status")
        uid = result.get("uid")
        reason = result.get("reason", "")
        http_status = result.get("http_status")
    else:
        return FacebookLiveCheckResult(
            FACEBOOK_CHECK_ERROR,
            reason="Kết quả kiểm tra Facebook token không hợp lệ.",
        )

    status = str(status or "").strip().upper()
    uid = uid.strip() if isinstance(uid, str) and uid.strip() else None
    reason = _safe_display_message(_mask_sensitive_text(reason))
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        http_status = None
    if status == FACEBOOK_LIVE and uid:
        return FacebookLiveCheckResult(
            status, uid, reason or "Facebook token LIVE", http_status
        )
    if status == FACEBOOK_DIE_TOKEN:
        return FacebookLiveCheckResult(
            status,
            reason=reason or "Facebook token đã hết hạn hoặc không hợp lệ.",
            http_status=http_status,
        )
    return FacebookLiveCheckResult(
        FACEBOOK_CHECK_ERROR,
        reason=reason or "Không thể xác minh Facebook token.",
        http_status=http_status,
    )


class AUTO_Action:
    def __init__(self, log_fn=None):
        self.url = 'https://b-graph.facebook.com/graphql'
        self._requests = requests.Session()
        self._log_fn = log_fn
        self.last_comment_evidence = _facebook_comment_response_evidence(None)
        self.last_reaction_evidence = {
            "success": False,
            "classification": "NOT_ATTEMPTED",
            "attempts": 0,
        }
        self.id_reaction = {
            'LIKE': 1635855486666999,
            'LOVE': 1678524932434102,
            'CARE': 613557422527858,
            'HAHA': 115940658764963,
            'WOW': 478547315650144,
            'SAD': 908563459236466,
            'ANGRY': 444813342392137,
        }
        self._requests.headers.update({
            "User-Agent": "[FBAN/FB4A;FBAV/571.0.0.44.73;FBBV/1023572511;FBDM/{density=1.5,width=540,height=960};FBLC/en_US;FBRV/0;FBCR/Verizon;FBMF/samsung;FBBD/samsung;FBPN/com.facebook.katana;FBDV/SM-S901N;FBSV/9;FBOP/1;FBCA/x86_64:arm64-v8a;]",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Priority": "u=0",
            "app-scope-id-header": "77a256c1-5911-48a4-bafd-fa4316c9f941",
            "x-fb-client-ip": "True",
            "x-fb-conn-uuid-client": "AS4k/LynmUQ6y62aIVsNWw==",
            "X-FB-Connection-Type": "WIFI",
            "x-fb-device-group": "1904",
            "X-FB-HTTP-Engine": "Tigon/Liger",
            "X-FB-Net-HNI": "310005",
            "X-FB-Request-Analytics-Tags": '{"network_tags":{"product":"350685531728","request_category":"graphql","purpose":"fetch","retry_attempt":"0"},"application_tags":"graphservice"}',
            "x-fb-rmd": "fail=Server:INVALID_MAP,Default:INVALID_MAP;v=;ip=;tkn=;reqTime=1633641062;recvTime=0",
            "x-fb-server-cluster": "True",
            "X-FB-SIM-HNI": "310005",
            "x-graphql-client-library": "graphservice",
            "x-graphql-request-purpose": "fetch",
            "X-Meta-USDID": "28266e60-2c0c-48f2-b64d-d25fee3d714e.1785300592.MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAELj5WOTDR3Uvqc5tqJ5lWdL_Mdi5xxeE342NEI82WLzzDIBKX4Q6wQ7hwtZDInM4tf_pkkNKKsoBQo_2cQQbIvQ.MEUCIQCx1YdU9dWWQRQ-bxEA83dBGnCWy5pAm3ed5KQUnoQlSAIgX_ISrCeup82fNh7OBMl-jZQ18eNbmooTjFzt6Ne12XY",
            "X-Meta-ZCA": "empty_token",
            "x-tigon-is-retry": "False",
            "x-zero-eh": "664c0faaac849cb891d0a261fbb72a12",
            "X-ZERO-STATE": "unknown",
            "X-ZERO-F-DEVICE-ID": '77a256c1-5911-48a4-bafd-fa4316c9f941'
        })

    def set_log_fn(self, log_fn):
        self._log_fn = log_fn

    def _log(self, message, level="info"):
        message = _mask_sensitive_text(message)
        if self._log_fn is not None:
            self._log_fn(message, level)
            return
        printers = {
            "success": print_success,
            "warning": print_warning,
            "error": print_error,
            "raw": print,
        }
        printers.get(level, print_info)(message)

    def check_live(self, token):
        client_trace_id = str(uuid.uuid4())
        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "FbStoriesLightBucketsQuery",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '134400997213594131792470699723',
            'fb_api_client_context': '{"is_background":false}',
            'variables': r'''{"video_thumbnail_width":336,"profile_image_size":60,"skip_top_tray_ats_mid_card_offset":true,"story_media_height":960,"skip_inorganic_buckets":true,"story_media_width":540,"use_native_entrypoint_for_stars_on_reels":true,"enable_hd":true,"scale":"1.5","nt_context":{"using_white_navbar":true,"styles_id":"a07b73c926a84224348806e6cd486365","pixel_ratio":1.5,"is_push_on":true,"debug_tooling_metadata_token":null,"is_flipper_enabled":false,"theme_params":[{"value":["three_neutral_gray"],"design_system_name":"XMDS"},{"value":["DARKER_PRIMARY_DEEMPHASIZED_BUTTON_BACKGROUND_TEST"],"design_system_name":"FDS"}],"bloks_version":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"},"use_server_thumbnail":true,"enable_content_sharing_in_notes":true,"enable_uses_suggested_lyrics":true,"is_final_ranking_score_enabled":true,"enable_gif_note":true,"fbstory_tray_preview_height":295,"msqrd_supported_capabilities":[{"value":"multiplane_disabled","name":"multiplane"},{"value":"world_tracker_enabled","name":"world_tracker"},{"value":"xray_disabled","name":"xray"},{"value":"world_tracking_disabled","name":"world_tracking"},{"value":"half_float_render_pass_enabled","name":"half_float_render_pass"},{"value":"multiple_render_targets_enabled","name":"multiple_render_targets"},{"value":"vertex_texture_fetch_enabled","name":"vertex_texture_fetch"},{"value":"render_settings_high_enabled","name":"render_settings_high"},{"value":"body_tracking_enabled","name":"body_tracking"},{"value":"gyroscope_enabled","name":"gyroscope"},{"value":"geoanchor_disabled","name":"geoanchor"},{"value":"scene_depth_disabled","name":"scene_depth"},{"value":"182,202","name":"supported_beta_sdk_versions"},{"value":"segmentation_enabled","name":"segmentation"},{"value":"hand_tracking_enabled","name":"hand_tracking"},{"value":"real_scale_estimation_disabled","name":"real_scale_estimation"},{"value":"hair_segmentation_disabled","name":"hair_segmentation"},{"value":"depth_shader_read_enabled","name":"depth_shader_read"},{"value":"etc2_compression","name":"compression"},{"value":"0","name":"face_tracker_version"},{"value":"149.0,150.0,151.0,152.0,153.0,154.0,155.0,156.0,157.0,158.0,159.0,160.0,161.0,162.0,163.0,164.0,165.0,166.0,167.0,168.0,169.0,170.0,171.0,172.0,173.0,174.0,175.0,176.0,177.0,178.0,179.0,180.0,181.0,182.0,183.0,184.0,185.0,186.0,187.0,188.0,189.0,190.0,191.0,192.0,193.0,194.0,195.0,196.0,197.0,198.0,199.0,200.0,201.0,202.0","name":"supported_sdk_versions"}],"min_story_page_size_from_client":4,"enable_status_consumption":true,"should_include_first_media":true,"video_thumbnail_height":336,"fetch_bucket_content_hash":true,"should_fetch_midcard_ranking_tracking_string":true,"fbstory_tray_sizing_type":"cover-fill-cropped","should_fetch_model_for_thumbnail_frame":true,"fbstory_tray_preview_width":166,"enable_cix_screen_rollout":true,"client_request_id":"4175cf24-7ea8-4a9a-8b1e-91f9f0da9077","is_hybrid_tray_enabled":true,"should_fetch_is_trending_bucket_field":true,"page_profile_image_size_experiment":60,"should_include_production_pog_data":true,"should_include_expiration_time":true,"should_include_feedback_summary":true,"should_include_live_media":true,"should_include_live_tile_metadata":true,"show_production_pog_image":true,"unified_stories_buckets_paginated_for_light_query_first":6,"should_fetch_profile_navigation_info":true,"should_skip_feedback_total_count":true,"should_include_secondary_text_for_non_self_pog":true,"enable_easter_egg_v2":true,"include_inform_treatment":true,"compose_surface":"STORIES"}''',
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }
        for attempt in range(2):
            try:
                res = self._requests.post(url=self.url, headers={
                    "Authorization": f"OAuth {token}",
                    "X-FB-Friendly-Name": "FbStoriesLightBucketsQuery",
                }, data=payload, timeout=FB_READ_TIMEOUT)
                break
            except requests.RequestException as exc:
                if attempt == 0:
                    self._log(
                        f"Facebook get_info timeout/lỗi mạng: {exc}; thử lại lần cuối...",
                        "warning",
                    )
                    time.sleep(NETWORK_RETRY_DELAY)
                    continue
                return FacebookLiveCheckResult(
                    FACEBOOK_CHECK_ERROR,
                    reason=_safe_display_message(_mask_sensitive_text(
                        f"Facebook get_info timeout/lỗi mạng: {exc}"
                    )),
                )
        http_status = getattr(res, "status_code", None)
        if not isinstance(http_status, int) or isinstance(http_status, bool):
            http_status = None
        try:
            res_json = res.json()
        except (TypeError, ValueError) as exc:
            return FacebookLiveCheckResult(
                FACEBOOK_CHECK_ERROR,
                reason=_safe_display_message(_mask_sensitive_text(
                    f"Lỗi parse JSON khi kiểm tra Facebook token: {exc}"
                )),
                http_status=http_status,
            )
        if not isinstance(res_json, dict):
            return FacebookLiveCheckResult(
                FACEBOOK_CHECK_ERROR,
                reason="Facebook trả về dữ liệu identity không hợp lệ.",
                http_status=http_status,
            )
        if "errors" in res_json:
            errors = res_json.get("errors")
            if not isinstance(errors, list):
                errors = []
            messages = []
            for err in errors:
                if not isinstance(err, dict):
                    continue
                message = str(err.get("message") or "").strip()
                if message:
                    messages.append(message)
                if "access_token" in message.lower():
                    return FacebookLiveCheckResult(
                        FACEBOOK_DIE_TOKEN,
                        reason="Facebook token đã hết hạn hoặc không hợp lệ.",
                        http_status=http_status,
                    )
            return FacebookLiveCheckResult(
                FACEBOOK_CHECK_ERROR,
                reason=_safe_display_message(_mask_sensitive_text(
                    messages[0] if messages else "Lỗi GraphQL không xác định."
                )),
                http_status=http_status,
            )
        response_data = res_json.get("data") if isinstance(res_json, dict) else None
        me = response_data.get("me") if isinstance(response_data, dict) else None
        user_id = me.get("id") if isinstance(me, dict) else None
        if isinstance(user_id, str) and user_id.strip():
            return FacebookLiveCheckResult(
                FACEBOOK_LIVE,
                uid=user_id.strip(),
                reason="Facebook token LIVE",
                http_status=http_status,
            )
        return FacebookLiveCheckResult(
            FACEBOOK_CHECK_ERROR,
            reason="Không tìm thấy user_id trong Facebook identity response.",
            http_status=http_status,
        )

    def get_info(self, token):
        """Compatibility adapter for existing CLI and external callers."""
        result = self.check_live(token)
        if result.status == FACEBOOK_LIVE:
            return result.uid, "OK"
        return None, result.reason

    def close(self):
        self._requests.close()

    @staticmethod
    def _reaction_error_text(error):
        if not isinstance(error, dict):
            return "", ""
        summary = _safe_display_message(error.get("summary"), limit=300)
        description = _safe_display_message(error.get("description"), limit=500)
        return summary, description

    @staticmethod
    def _is_transient_reaction_error(summary, description):
        normalized_summary = " ".join(str(summary or "").casefold().split())
        normalized_description = " ".join(
            str(description or "").casefold().split()
        )
        return (
            normalized_summary == FACEBOOK_REACTION_TRANSIENT_SUMMARY
            or FACEBOOK_REACTION_TRANSIENT_DESCRIPTION in normalized_description
        )

    def _reaction_attempt(self, payload, token):
        try:
            response = self._requests.post(url=self.url, headers={
                "Authorization": f"OAuth {token}",
                "X-FB-Friendly-Name": "ViewerReactionsMutation",
            }, data=payload, timeout=FB_ACTION_TIMEOUT)
        except requests.RequestException as exc:
            return FacebookReactionAttemptResult(
                False,
                "NETWORK_UNKNOWN",
                description=_safe_display_message(
                    _mask_sensitive_text(str(exc)), limit=500
                ),
            )

        response_text = str(getattr(response, "text", "") or "")
        try:
            result = response.json()
        except (TypeError, ValueError) as exc:
            return FacebookReactionAttemptResult(
                False,
                "PARSE_ERROR",
                description=_safe_display_message(str(exc), limit=300),
                raw_text=response_text[:500],
            )
        if not isinstance(result, dict):
            return FacebookReactionAttemptResult(
                False,
                "PARSE_ERROR",
                description="Facebook trả về dữ liệu reaction không hợp lệ.",
                raw_text=response_text[:500],
            )

        errors = result.get("errors")
        if errors is not None:
            if not isinstance(errors, list):
                errors = []
            blocked_message = _facebook_interaction_block_message(errors)
            if blocked_message is not None:
                raise FacebookInteractionBlocked(blocked_message)
            error_texts = [
                self._reaction_error_text(item)
                for item in errors
                if isinstance(item, dict)
            ]
            error_texts = [pair for pair in error_texts if any(pair)]
            summary, description = error_texts[0] if error_texts else ("", "")
            classification = (
                "TRANSIENT_RESPONSE"
                if error_texts and all(
                    self._is_transient_reaction_error(item_summary, item_description)
                    for item_summary, item_description in error_texts
                )
                else "NON_RETRYABLE_RESPONSE"
            )
            raw_text = "\n".join(
                " — ".join(part for part in pair if part)
                for pair in error_texts
            )
            if not raw_text:
                raw_text = _safe_display_message(response_text, limit=500)
            return FacebookReactionAttemptResult(
                False,
                classification,
                summary=summary,
                description=description,
                raw_text=raw_text,
            )

        data = result.get("data")
        feedback_react = data.get("feedback_react") if isinstance(data, dict) else None
        feedback = (
            feedback_react.get("feedback")
            if isinstance(feedback_react, dict)
            else None
        )
        if isinstance(feedback, dict) and (
                feedback.get("viewer_feedback_reaction_key") is not None):
            return FacebookReactionAttemptResult(True, "SUCCESS")
        return FacebookReactionAttemptResult(
            False,
            "NON_RETRYABLE_RESPONSE",
            description="Phản hồi Facebook thiếu dữ liệu xác nhận reaction.",
            raw_text=_safe_display_message(response_text, limit=500),
        )

    def auto_reaction(self, post_id, reaction_type, token, actor_id):
        raw = f"feedback:{post_id}"
        feedback_id = base64.b64encode(raw.encode()).decode()
        fragments = [
            generate_fragment("NewsFeedFragment", "native_newsfeed", duration=55.0),
            generate_fragment("PermalinkDialogFragment", "story_view", duration=0.0)
        ]
        attribution_str = build_attribution(fragments)
        session_id = generate_session_id()
        ts = int(time.time())
        client_trace_id = str(uuid.uuid4())
        variables = {
            "profile_image_size": 60,
            "input": {
                "tracking": None,
                "session_id": session_id,
                "feedback_source": "native_newsfeed",
                "feedback_referrer": "native_newsfeed",
                "feedback_id": feedback_id,
                "feedback_reaction_id": self.id_reaction[reaction_type],
                "downstream_share_session_id": None,
                "downstream_share_session_start_time": None,
                "downstream_share_session_origin_uri": None,
                "nectar_module": "newsfeed_ufi",
                "attribution_id_v2": attribution_str,
                "actor_id": actor_id,
                "downstream_share_session_navigation_events_count": [
                    {"name": "APP_BACKGROUNDED", "count": 0},
                    {"name": "APP_FOREGROUNDED", "count": 0}
                ],
                "action_timestamp": ts
            },
            "is_comment_sharing_enabled": True
        }
        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "ViewerReactionsMutation",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '285778409311568856348218907304',
            'fb_api_client_context': json.dumps({"is_background": False}),
            'variables': json.dumps(variables),
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }
        attempts = 0
        attempt_result = None
        while attempts < 2:
            attempts += 1
            attempt_result = self._reaction_attempt(payload, token)
            if attempt_result.raw_text:
                label = "Facebook thử lại" if attempts == 2 else "Facebook"
                self._log(f"{label}:\n{attempt_result.raw_text}", "raw")
            if attempt_result.success:
                break
            if not (
                attempts == 1
                and attempt_result.classification == "TRANSIENT_RESPONSE"
            ):
                break
            details = " — ".join(
                part for part in (
                    attempt_result.summary,
                    attempt_result.description,
                ) if part
            )
            details_text = f": {details}" if details else ""
            if details_text and details_text[-1] not in ".!?":
                details_text += "."
            self._log(
                f"Facebook {reaction_type} trả lỗi tạm thời"
                f"{details_text} "
                f"Thử lại reaction 1 lần sau {NETWORK_RETRY_DELAY} giây.",
                "warning",
            )
            time.sleep(NETWORK_RETRY_DELAY)

        self.last_reaction_evidence = {
            "success": bool(attempt_result and attempt_result.success),
            "classification": (
                attempt_result.classification if attempt_result is not None
                else "NOT_ATTEMPTED"
            ),
            "attempts": attempts,
            "summary": attempt_result.summary if attempt_result is not None else "",
            "description": (
                attempt_result.description if attempt_result is not None else ""
            ),
        }
        return self.last_reaction_evidence["success"]

    def auto_cmt(self, post_id, text, token, actor_id):
        self.last_comment_evidence = _facebook_comment_response_evidence(None)
        raw = f"feedback:{post_id}"
        feedback_id = base64.b64encode(raw.encode()).decode()
        fragments = [
            generate_fragment("NewsFeedFragment", "native_newsfeed", duration=55.0),
            generate_fragment("PermalinkDialogFragment", "story_view", duration=0.0)
        ]
        attribution_str = build_attribution(fragments)
        session_id = generate_session_id()
        ts = int(time.time())
        client_trace_id = str(uuid.uuid4())
        idempotence_token = str(uuid.uuid4())

        variables = {
            "query_for_can_be_invited_by_viewer": True,
            "image_medium_width": 270,
            "image_low_height": 2048,
            "image_medium_height": 2048,
            "profile_image_size": 60,
            "media_type": "image/x-auto",
            "image_high_width": 540,
            "input": {
                "tracking": None,
                "skip_link_usage_quota_validation": False,
                "session_id": session_id,
                "privacy_type": "DEFAULT_PRIVACY",
                "feedback_source": "feedback_comments",
                "vod_video_timestamp": 0,
                "downstream_share_session_id": None,
                "downstream_share_session_start_time": None,
                "downstream_share_session_origin_uri": None,
                "nectar_module": "newsfeed_ufi",
                "actor_id": actor_id,
                "attribution_id_v2": attribution_str,
                "action_timestamp": ts,
                "ad_imp_token": None,
                "message": {
                    "text": text,
                    "ranges": []
                },
                "feedback_referrer": "native_newsfeed",
                "feedback_id": feedback_id,
                "idempotence_token": idempotence_token,
                "entry_point": "TAP_FOOTER_COMMENT",
                "downstream_share_navigation_events_count": [
                    {"name": "APP_BACKGROUNDED", "count": 0},
                    {"name": "APP_FOREGROUNDED", "count": 0}
                ]
            },
            "enable_participation_questionnaire": True,
            "enable_reactions_dock_message": True,
            "image_low_width": 180,
            "enable_user_signals_in_comments": True,
            "enable_comment_shares": True,
            "nt_context": {
                "using_white_navbar": True,
                "styles_id": "a07b73c926a84224348806e6cd486365",
                "pixel_ratio": 1.5,
                "is_push_on": True,
                "debug_tooling_metadata_token": None,
                "is_flipper_enabled": False,
                "theme_params": [
                    {"value": ["three_neutral_gray"], "design_system_name": "XMDS"},
                    {"value": ["DARKER_PRIMARY_DEEMPHASIZED_BUTTON_BACKGROUND_TEST"], "design_system_name": "FDS"}
                ],
                "bloks_version": "64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"
            },
            "fetch_presence_eligible": True,
            "image_high_height": 2048,
            "fetch_privacy_value_for_pending_approval_comment": True,
            "fetch_privacy_value_for_declined_comment": True,
            "should_fetch_pinned_comment": True,
            "enable_story_ring": True,
            "should_fetch_content_owner_reaction": True,
            "group_member_action_source": "GROUP_COMMENT",
            "feedback_source": "NEWS_FEED",
            "enable_add_friend_in_comments": True,
            "should_fetch_comments_downvote_fields": True,
            "size_style": "contain-fit",
            "enable_identity_badge_v2": True,
            "enable_comment_identity_badge": True,
            "profile_pic_media_type": "image/x-auto",
            "enable_comment_pin_v2": True,
            "enable_gen_ai_content_transparency_in_comments": True
        }

        variables_str = json.dumps(variables, ensure_ascii=False)
        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "CommentCreateMutation",
            "fb_api_caller_class": "graphservice",
            "client_doc_id": "8474489818219302131872116307",
            "fb_api_client_context": json.dumps({"is_background": False}),
            "variables": variables_str,
            "fb_api_analytics_tags": r'''["nav_attribution_id_v2=%s","GraphServices","surface_hierarchy=FeedbackFragment,story_feedback_flyout,null;SimpleUFIPopoverFragment,story_feedback_flyout,null;NewsFeedFragment,native_newsfeed,null;FbChromeFragment,unknown,null;FbMainTabActivity,native_newsfeed,null","visitation_id=null","session_id=%s"]''' % (attribution_str,session_id),
            "client_trace_id": client_trace_id
        }
        try:
            res = self._requests.post(url=self.url, headers={
                "Authorization": f"OAuth {token}",
                "X-FB-Friendly-Name": "CommentCreateMutation",
            }, data=payload, timeout=FB_ACTION_TIMEOUT)
        except requests.RequestException as exc:
            self._log(f"Facebook comment timeout/lỗi mạng: {exc}; outcome unknown", "error")
            return False
        try:
            result = res.json()
        except:
            self._log("Lỗi parse JSON phản hồi Facebook comment.", "error")
            self._log(res.text[:500], "raw")
            return False
        if "errors" in result:
            blocked_message = _facebook_interaction_block_message(result.get("errors"))
            if blocked_message is not None:
                raise FacebookInteractionBlocked(blocked_message)
            for err in result["errors"]:
                summary, description = self._reaction_error_text(err)
                raw_text = " — ".join(
                    part for part in (summary, description) if part
                )
                if raw_text:
                    self._log(f"Facebook:\n{raw_text}", "raw")
            return False
        else:
            comment_data = result.get("data", {}).get("comment_create", {}).get("comment")
            evidence = _facebook_comment_response_evidence(comment_data)
            evidence["outgoing_text"] = text
            self.last_comment_evidence = evidence
            comment_id = evidence["comment_id"]
            parent_post_id = evidence["parent_post_id"]
            requested_text = unicodedata.normalize("NFC", text)
            returned_text_matches = all(
                unicodedata.normalize("NFC", returned_text) == requested_text
                for _field, returned_text in evidence["returned_text_fields"]
            )
            returned_actor_matches = all(
                returned_actor_id == str(actor_id)
                for _field, returned_actor_id in evidence["returned_actor_fields"]
            )
            parent_matches = parent_post_id is None or parent_post_id == str(post_id)
            if comment_id and parent_matches and returned_text_matches and returned_actor_matches:
                self._log(f"Comment thành công! ID: {comment_data['id']}", "success")
                return True
            else:
                self._log("Phản hồi thành công nhưng thiếu dữ liệu comment.", "warning")
                return False

# ---------- Hàm lấy post ID ----------
def get_facebook_post_id(url, log_fn=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    }
    with requests.Session() as session:
        session.headers.update(headers)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                resp = session.get(url, timeout=15, allow_redirects=True)
                if resp.status_code != 200:
                    failure = f"Lỗi HTTP {resp.status_code} khi lấy Post ID"
                else:
                    html = resp.text
                    try:
                        uid = html.split('"post_id":"')[1].split('",')[0]
                    except (AttributeError, IndexError):
                        if log_fn is None:
                            print("Không tìm thấy Post ID trong phản hồi Facebook.")
                        else:
                            log_fn("Không tìm thấy Post ID trong phản hồi Facebook.", "warning")
                        return None
                    if log_fn is None:
                        print(f"{Fore.GREEN}{Style.BRIGHT}✅ Post ID: {uid}{Style.RESET_ALL}")
                    else:
                        log_fn(f"Post ID: {uid}", "success")
                    return uid
            except requests.RequestException as exc:
                failure = f"Lấy Post ID timeout/lỗi mạng: {exc}"

            if attempt < max_attempts:
                message = f"{failure}, thử lại {attempt + 1}/{max_attempts}..."
                if log_fn is None:
                    print(message)
                else:
                    log_fn(message, "warning")
                time.sleep(NETWORK_RETRY_DELAY)
            else:
                message = f"{failure}; đã hết {max_attempts} lần thử."
                if log_fn is None:
                    print(message)
                else:
                    log_fn(message, "error")
        return None


def _resolve_ttc_facebook_post_id(job, log_fn=None):
    """Use a valid direct ID first, otherwise resolve the normalized object."""
    if job.get("ttc_source") in TTC_DIRECT_POST_ID_JOB_TYPES:
        post_id = job.get("post_id")
        if isinstance(post_id, str) and re.fullmatch(r"[0-9]+", post_id) is not None:
            if log_fn is None:
                print(f"{Fore.GREEN}{Style.BRIGHT}✅ Post ID: {post_id}{Style.RESET_ALL}")
            else:
                log_fn(f"Post ID: {post_id}", "success")
            return post_id

    object_id = job.get("object_id")
    return get_facebook_post_id(
        f"https://www.facebook.com/{object_id}", log_fn=log_fn
    )

# ---------- Hàm đếm delay ----------
def countdown(seconds, message="Chờ"):
    for i in range(seconds, 0, -1):
        print(f"\r{Fore.YELLOW}⏳ {message} {i} giây...{Style.RESET_ALL}", end="", flush=True)
        time.sleep(1)
    print(f"\r{Fore.GREEN}✅ Tiếp tục!{' ' * 20}{Style.RESET_ALL}")

def parse_facebook_login_batch(raw):
    raw = str(raw or "")
    segments = raw.split(',')
    if len(segments) == 1 and not segments[0].strip():
        raise ValueError("Danh sách account không được để trống.")

    accounts = []
    seen_identities = {}
    for position, segment in enumerate(segments, start=1):
        if not segment.strip():
            raise ValueError(f"Account vị trí {position} bị trống.")

        fields = segment.split('|')
        if len(fields) != 3:
            raise ValueError(
                f"Account vị trí {position} phải có đúng định dạng UID|PASSWORD|2FA."
            )

        email, password, secret = (field.strip() for field in fields)
        if not email:
            raise ValueError(f"UID/email ở vị trí {position} không được để trống.")
        if not password:
            raise ValueError(f"Password ở vị trí {position} không được để trống.")

        identity = email.casefold()
        if identity in seen_identities:
            first_position = seen_identities[identity]
            raise ValueError(
                f"UID/email {email} ở vị trí {position} bị trùng với vị trí {first_position}."
            )
        seen_identities[identity] = position
        accounts.append({"email": email, "password": password, "secret": secret})

    return accounts


def _is_duplicate_account(accounts, candidate, ignore_index=None):
    candidate_email = str(candidate.get('email', '')).strip().casefold()
    candidate_token = str(candidate.get('token', '')).strip()
    for index, account in enumerate(accounts):
        if ignore_index is not None and index == ignore_index:
            continue
        existing_email = str(account.get('email', '')).strip().casefold()
        existing_token = str(account.get('token', '')).strip()
        if candidate_email and existing_email == candidate_email:
            return True
        if candidate_token and existing_token == candidate_token:
            return True
    return False


def _include_new_account_in_explicit_selection(profile, account):
    selected = profile.get('run_config', {}).get('selected_fb_account_keys')
    if selected is not None:
        key = facebook_account_key(account)
        if key and key not in selected:
            selected.append(key)


def _persist_login_account(profile_id, email, password, secret, token):
    account = {
        "email": email,
        "password": password,
        "secret": secret,
        "token": token,
    }

    def _mutator(data):
        prof = get_profile(data, profile_id)
        if not prof:
            return "profile_missing"
        if _is_duplicate_account(prof.get('fb_accounts', []), account):
            return "duplicate"
        prof.setdefault('fb_accounts', []).append(account)
        _include_new_account_in_explicit_selection(prof, account)
        return "success"

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def add_manual_account(profile_id, token):
    token = str(token or '').strip()
    if not token:
        print_error("Token Facebook không được để trống.")
        return False

    print("\nToken vừa nhập:\n")
    print(token)
    if input("\nThêm Facebook token này? (y/n): ").strip().lower() not in ['y', 'yes']:
        print_info("Đã hủy.")
        return False

    def _mutator(data):
        prof = get_profile(data, profile_id)
        if not prof:
            raise ValueError("Không tìm thấy profile")
        account = {"token": token, "email": "", "password": "", "secret": ""}
        if _is_duplicate_account(prof.get('fb_accounts', []), account):
            raise ValueError("Facebook account/token đã tồn tại trong TTC profile này.")
        prof.setdefault('fb_accounts', []).append(account)
        _include_new_account_in_explicit_selection(prof, account)
        return True

    try:
        with profile_run_lock(profile_id):
            mutate_database(_mutator)
            return True
    except RuntimeError as e:
        print_error(str(e))
        return False
    except ValueError as e:
        print_warning(str(e))
        return False


def add_login_account(profile_id, email, password, secret="", req_fb=None):
    email = str(email or '').strip()
    if not email:
        print_error("Email / UID không được để trống.")
        return False

    owns_client = req_fb is None
    login_client = Requests_facebook() if owns_client else req_fb
    try:
        token = login_client.login_and_get_token(email, password, secret)
    finally:
        if owns_client:
            login_client.close()
    if not token:
        print_error(f"Đăng nhập thất bại cho {email}.")
        return False

    try:
        persist_status = _persist_login_account(profile_id, email, password, secret, token)
        if persist_status == "profile_missing":
            print_warning("Không tìm thấy profile")
            return False
        if persist_status == "duplicate":
            print_warning("Facebook account đã tồn tại trong TTC profile này.")
            return False
        if persist_status == "success":
            print_success(f"Đã thêm Facebook account {email}.")
            return True
    except RuntimeError as e:
        print_error(str(e))
        return False
    except ValueError as e:
        print_warning(str(e))
        return False


def batch_login_accounts(
        profile_id, raw_credentials, delay_seconds=15,
        req_fb_factory=None, countdown_func=None):
    accounts = parse_facebook_login_batch(raw_credentials)
    countdown_func = countdown_func or countdown
    total = len(accounts)
    results = []
    stop_batch = False

    for index, account in enumerate(accounts, start=1):
        email = account["email"]
        try:
            profile_exists = get_profile(load_database(), profile_id) is not None
        except Exception:
            print_error("Không thể đọc dữ liệu profile mới nhất. Dừng batch login.")
            results.append({"email": email, "status": "persistence_failed"})
            stop_batch = True
            break
        if not profile_exists:
            print_error("TTC profile không còn tồn tại. Dừng batch login.")
            results.append({"email": email, "status": "profile_missing"})
            stop_batch = True
            break

        print_info(f"[{index}/{total}] Đang login UID/email {email}...")
        try:
            req_fb = req_fb_factory() if req_fb_factory is not None else Requests_facebook()
            try:
                token = req_fb.login_and_get_token(
                    email, account["password"], account["secret"]
                )
            finally:
                if req_fb_factory is None:
                    req_fb.close()
        except Exception:
            token = None

        if not token:
            print_error(f"[{index}/{total}] Login thất bại cho {email}.")
            results.append({"email": email, "status": "login_failed"})
            try:
                if get_profile(load_database(), profile_id) is None:
                    print_error("TTC profile không còn tồn tại. Dừng batch login.")
                    stop_batch = True
            except Exception:
                print_error("Không thể đọc dữ liệu profile mới nhất. Dừng batch login.")
                stop_batch = True
        else:
            try:
                persist_status = _persist_login_account(
                    profile_id, email, account["password"], account["secret"], token
                )
            except ProfileInUseError:
                print_error(
                    f"[{index}/{total}] Không thể thêm {email} vì TTC profile đang được sử dụng."
                )
                persist_status = "profile_busy"
            except Exception:
                print_error(f"[{index}/{total}] Không thể lưu account {email}.")
                persist_status = "persistence_failed"

            results.append({"email": email, "status": persist_status})
            if persist_status == "success":
                print_success(f"[{index}/{total}] Đã thêm Facebook account {email}.")
            elif persist_status == "duplicate":
                print_warning(f"[{index}/{total}] Facebook account {email} đã tồn tại.")
            elif persist_status == "profile_missing":
                print_error("TTC profile không còn tồn tại. Dừng batch login.")
                stop_batch = True
            elif persist_status in {"profile_busy", "persistence_failed"}:
                stop_batch = True

        if stop_batch:
            break
        if index < total:
            countdown_func(
                delay_seconds,
                "Chờ trước lần login Facebook tiếp theo"
            )

    if stop_batch and len(results) < total:
        for account in accounts[len(results):]:
            results.append({"email": account["email"], "status": "stopped"})

    success_count = sum(item["status"] == "success" for item in results)
    skipped_count = sum(item["status"] in {"duplicate", "stopped"} for item in results)
    failed_count = total - success_count - skipped_count
    print(f"\n{Fore.YELLOW}=== KẾT QUẢ BATCH LOGIN ==={Style.RESET_ALL}")
    print(f"Tổng: {total}")
    print(f"Thành công: {success_count}")
    print(f"Thất bại: {failed_count}")
    print(f"Bỏ qua/trùng: {skipped_count}")

    status_labels = {
        "success": "✅ Thành công",
        "login_failed": "❌ Login thất bại",
        "duplicate": "⚠️ Đã tồn tại",
        "profile_missing": "❌ Profile không còn tồn tại",
        "profile_busy": "❌ Profile đang được sử dụng",
        "persistence_failed": "❌ Không thể lưu",
        "stopped": "⚠️ Chưa xử lý do batch đã dừng",
    }
    for index, item in enumerate(results, start=1):
        print(f"{index}. UID/email {item['email']} — {status_labels[item['status']]}")

    return {
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "results": results,
    }


def view_accounts(profile):
    accounts = profile.get('fb_accounts', [])
    selected = set(selected_account_keys(profile))
    print(f"\n{Fore.YELLOW}Facebook của TTC: {profile['name']}{Style.RESET_ALL}")
    if not accounts:
        print_info("Chưa có Facebook account.")
        return
    for index, account in enumerate(accounts, start=1):
        key = facebook_account_key(account)
        run_mark = "✅" if key in selected else "❌"
        identity = str(account.get('email', '')).strip() or "Manual token"
        print(f"[{index}] {run_mark} {identity}")


def show_facebook_account_details(profile):
    index = _account_index(profile, "Chọn index account để xem chi tiết: ")
    if index is None:
        return
    account = profile['fb_accounts'][index]
    key = facebook_account_key(account)
    selected = set(selected_account_keys(profile))
    run_status = "✅ Sẽ chạy" if key in selected else "❌ Không chạy"
    identity = str(account.get('email', '')).strip() or "Manual token"

    print(f"\n{Fore.YELLOW}=== CHI TIẾT FACEBOOK ==={Style.RESET_ALL}")
    print(f"Profile:\n{profile['name']}\n")
    print(f"UID/email:\n{identity}\n")
    print(f"Trạng thái:\n{run_status}\n")
    print("Token:\n")
    print(f"{account.get('token', '')}\n")
    print("2FA:")
    print("Có" if str(account.get('secret', '')).strip() else "Không")
    print("\nPassword:")
    print("Không hiển thị trong màn hình thường.\n")


def _account_index(profile, prompt="Chọn index Facebook account: "):
    accounts = profile.get('fb_accounts', [])
    if not accounts:
        print_warning("Chưa có Facebook account.")
        return None
    view_accounts(profile)
    raw = input(prompt).strip()
    try:
        index = int(raw) - 1
    except ValueError:
        print_error("Index không hợp lệ.")
        return None
    if index < 0 or index >= len(accounts):
        print_error("Index không hợp lệ.")
        return None
    return index


def edit_account(profile):
    index = _account_index(profile)
    if index is None:
        return
    account = profile['fb_accounts'][index]
    old_key = facebook_account_key(account)
    print_info("Enter trống = giữ nguyên. Với Password/2FA nhập '-' để xóa.")
    email = input(f"Email / UID [{account.get('email') or 'trống'}]: ").strip()
    token = input(f"Facebook token [{preview_token(account.get('token'))}] (trống=giữ): ").strip()
    password = secret_input("Password mới (trống=giữ, -=xóa): ")
    secret = secret_input("2FA secret mới (trống=giữ, -=xóa): ")

    updates = {}
    if email:
        updates['email'] = email
    if token:
        updates['token'] = token
    if password == '-':
        updates['password'] = ''
    elif password:
        updates['password'] = password
    if secret == '-':
        updates['secret'] = ''
    elif secret:
        updates['secret'] = secret

    if token:
        print("\nToken mới vừa nhập:\n")
        print(token)

    if input("\nLưu thay đổi? (y/n): ").strip().lower() not in ['y', 'yes']:
        print_info("Đã hủy.")
        return

    profile_id = profile.get("profile_id")
    def _mutator(data):
        prof = get_profile(data, profile_id)
        if not prof:
            raise ValueError("Không tìm thấy profile")

        # Locate account by old_key
        target_index = -1
        for i, acc in enumerate(prof.get('fb_accounts', [])):
            if facebook_account_key(acc) == old_key:
                target_index = i
                break

        if target_index == -1:
            raise ValueError("Facebook account không còn tồn tại trong dữ liệu mới nhất.")

        latest_account = prof['fb_accounts'][target_index]
        updated = dict(latest_account)
        updated.update(updates)
        if _is_duplicate_account(prof['fb_accounts'], updated, ignore_index=target_index):
            raise ValueError("Email hoặc token mới bị trùng với account khác.")

        for field, value in updates.items():
            latest_account[field] = value
        new_key = facebook_account_key(latest_account)
        selected = prof['run_config'].get('selected_fb_account_keys')
        if selected is not None and new_key != old_key:
            was_selected = old_key in selected
            selected[:] = [key for key in selected if key != old_key]
            if was_selected and new_key and new_key not in selected:
                selected.append(new_key)
        return True

    try:
        with profile_run_lock(profile_id):
            mutate_database(_mutator)
            print_success("Đã lưu thay đổi Facebook account.")
    except RuntimeError as e:
        print_error(str(e))
    except ValueError as e:
        print_error(str(e))


def relogin_account(profile):
    index = _account_index(profile)
    if index is None:
        return
    account = profile['fb_accounts'][index]
    old_key = facebook_account_key(account)
    email = str(account.get('email', '')).strip()
    if not email:
        print_error("Manual-token account không có email/UID để login lại.")
        return
    password = str(account.get('password', ''))
    secret = str(account.get('secret', ''))
    password_changed = False
    secret_changed = False
    if not password:
        password = secret_input("Password: ")
        password_changed = True
    use_saved_2fa = "y"
    if secret:
        use_saved_2fa = input("Dùng 2FA secret đã lưu? (Y/n): ").strip().lower() or "y"
    if use_saved_2fa != 'y':
        secret = secret_input("2FA secret (có thể trống): ")
        secret_changed = True
    login_client = Requests_facebook()
    try:
        token = login_client.login_and_get_token(email, password, secret)
    finally:
        login_client.close()
    if not token:
        print_error("Login lại thất bại; token cũ vẫn được giữ.")
        return

    profile_id = profile.get("profile_id")
    def _mutator(data):
        prof = get_profile(data, profile_id)
        if not prof:
            raise ValueError("Không tìm thấy profile")
        target_index = -1
        for i, acc in enumerate(prof.get('fb_accounts', [])):
            if facebook_account_key(acc) == old_key:
                target_index = i
                break
        if target_index == -1:
            raise ValueError("Facebook account không còn tồn tại trong dữ liệu mới nhất.")

        latest_account = prof['fb_accounts'][target_index]
        updated = dict(latest_account)
        if password_changed:
            updated['password'] = password
        if secret_changed:
            updated['secret'] = secret
        updated['token'] = token
        if _is_duplicate_account(prof['fb_accounts'], updated, ignore_index=target_index):
            raise ValueError("Token mới bị trùng với account khác.")
        prof['fb_accounts'][target_index] = updated
        new_key = facebook_account_key(updated)
        selected = prof['run_config'].get('selected_fb_account_keys')
        if selected is not None and new_key != old_key:
            was_selected = old_key in selected
            selected[:] = [key for key in selected if key != old_key]
            if was_selected and new_key and new_key not in selected:
                selected.append(new_key)
        return True

    try:
        with profile_run_lock(profile_id):
            mutate_database(_mutator)
            print_success("Login lại thành công và đã cập nhật Facebook token.")
    except RuntimeError as e:
        print_error(str(e))
    except ValueError as e:
        print_error(str(e))


def delete_account(profile):
    index = _account_index(profile)
    if index is None:
        return
    account = profile['fb_accounts'][index]
    old_key = facebook_account_key(account)
    identity = str(account.get('email', '')).strip() or "Manual token"

    print(f"\n{Fore.YELLOW}=== XÓA FACEBOOK ==={Style.RESET_ALL}")
    print(f"UID/email:\n{identity}\n")
    print(f"Token:\n{account.get('token', '')}\n")

    if input("Xóa account này? (y/n): ").strip().lower() not in ['y', 'yes']:
        print_info("Đã hủy.")
        return

    profile_id = profile.get("profile_id")
    def _mutator(data):
        prof = get_profile(data, profile_id)
        if not prof:
            raise ValueError("Không tìm thấy profile")
        target_index = -1
        for i, acc in enumerate(prof.get('fb_accounts', [])):
            if facebook_account_key(acc) == old_key:
                target_index = i
                break
        if target_index == -1:
            raise ValueError("Facebook account không còn tồn tại trong dữ liệu mới nhất.")

        del prof['fb_accounts'][target_index]
        selected = prof['run_config'].get('selected_fb_account_keys')
        if selected is not None:
            selected[:] = [key for key in selected if key != old_key]
        return True

    try:
        with profile_run_lock(profile_id):
            mutate_database(_mutator)
            print_success("Đã xóa Facebook account.")
    except RuntimeError as e:
        print_error(str(e))
    except ValueError as e:
        print_error(str(e))


def parse_account_selection(raw, account_count):
    raw = str(raw or '').strip()
    if not raw:
        return None
    raw_upper = raw.upper()
    if raw_upper == "ALL":
        return list(range(account_count))
    if raw_upper == "NONE":
        return []

    parts = [part.strip() for part in raw.split(',')]
    if any(not part for part in parts):
        return -1

    indices = []
    for p in parts:
        try:
            val = int(p)
            if val < 1 or val > account_count:
                return -1
            indices.append(val - 1)
        except ValueError:
            return -1

    deduped = []
    for idx in indices:
        if idx not in deduped:
            deduped.append(idx)
    return deduped


def apply_facebook_selection(profile_id, mode, snapshot_keys=None):
    mode = str(mode or '').upper()
    if mode not in {'ALL', 'NONE', 'SUBSET'}:
        raise ValueError("Chế độ chọn Facebook không hợp lệ.")
    requested_keys = list(snapshot_keys or [])

    def _mutator(data):
        prof = get_profile(data, profile_id)
        if not prof:
            raise ValueError("Không tìm thấy profile")

        latest_keys = [
            facebook_account_key(account)
            for account in prof.get('fb_accounts', [])
        ]
        latest_keys = [key for key in latest_keys if key]

        if mode == 'ALL':
            explicit = latest_keys
        elif mode == 'NONE':
            explicit = []
        else:
            allowed = set(latest_keys)
            if any(key not in allowed for key in requested_keys):
                raise ValueError(
                    "Danh sách Facebook đã thay đổi ở phiên khác.\n"
                    "Vui lòng mở lại danh sách và chọn lại."
                )
            explicit = requested_keys

        prof['run_config']['selected_fb_account_keys'] = explicit
        return {
            'selected_count': len(explicit),
            'latest_account_count': len(latest_keys),
        }

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def toggle_account_selection(profile):
    accounts = profile.get('fb_accounts', [])
    if not accounts:
        print_warning("Chưa có Facebook account.")
        return

    profile_id = profile.get("profile_id")
    view_accounts(profile)
    print("\n✅ = sẽ chạy\n❌ = không chạy\n")
    print("Nhập danh sách account muốn chạy, ví dụ: 1,2,4")
    print("ALL  = bật tất cả\nNONE = tắt tất cả\nEnter = hủy")

    raw = input("Chọn: ").strip()
    if not raw:
        return

    result = parse_account_selection(raw, len(accounts))
    if result == -1:
        print_error("Danh sách account không hợp lệ.")
        return
    elif result is None:
        return

    raw_upper = raw.upper()
    if raw_upper == 'ALL':
        mode = 'ALL'
        snapshot_keys = None
    elif raw_upper == 'NONE':
        mode = 'NONE'
        snapshot_keys = None
    else:
        mode = 'SUBSET'
        snapshot_keys = []
        for idx in result:
            key = facebook_account_key(accounts[idx])
            if key:
                snapshot_keys.append(key)

    try:
        committed = apply_facebook_selection(profile_id, mode, snapshot_keys)
        print_success(
            f"Đã chọn {committed['selected_count']}/"
            f"{committed['latest_account_count']} Facebook account để chạy."
        )
    except RuntimeError as e:
        print_error(str(e))
    except ValueError as e:
        print_error(str(e))
    latest_profile = get_profile(load_database(), profile_id)
    if latest_profile is not None:
        view_accounts(latest_profile)


def account_list_menu(profile):
    view_accounts(profile)
    if not profile.get('fb_accounts'):
        return
    while True:
        print("\n1. Xem chi tiết tài khoản")
        print("0. Quay lại")
        choice = input("Chọn (0-1): ").strip()
        if choice == '1':
            show_facebook_account_details(profile)
        elif choice == '0':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


def add_account_menu(profile_id):
    while True:
        print(f"\n{Fore.YELLOW}=== THÊM TÀI KHOẢN FACEBOOK ==={Style.RESET_ALL}")
        print("1. Login UID|PASSWORD|2FA")
        print("2. Thêm token thủ công")
        print("0. Quay lại")
        choice = input("Chọn (0-2): ").strip()
        if choice == '1':
            print("\nĐịnh dạng:")
            print("UID|PASSWORD|2FA")
            print("\nNhiều account:")
            print("UID|PASSWORD|2FA,UID|PASSWORD|2FA")
            print("\n2FA có thể để trống:")
            print("UID|PASSWORD|")
            raw_credentials = input("\nDán danh sách account: ")
            if not raw_credentials.strip():
                print_info("Đã hủy.")
                continue
            try:
                batch_login_accounts(profile_id, raw_credentials)
            except ValueError as exc:
                print_error(str(exc))
        elif choice == '2':
            token = input("Facebook token: ")
            if add_manual_account(profile_id, token):
                print_success("Đã thêm token.")
        elif choice == '0':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


def account_manager(profile_id):
    while True:
        try:
            profile = get_profile(load_database(), profile_id)
        except Exception as exc:
            print_error(f"Không thể đọc dữ liệu mới nhất. {exc}")
            return
        if profile is None:
            print_error("TTC profile không còn tồn tại trong dữ liệu mới nhất.")
            return
        accounts = profile.get('fb_accounts', [])
        enabled_count = len(selected_accounts(profile))
        print(f"\n{Fore.YELLOW}======== QUẢN LÝ FACEBOOK ========{Style.RESET_ALL}\n")
        print(f"Facebook: {enabled_count} bật / {len(accounts)} tổng\n")
        print("1. Xem danh sách tài khoản")
        print("2. Thêm tài khoản")
        print("3. Bật / tắt tài khoản")
        print("4. Login lại tài khoản")
        print("5. Sửa tài khoản")
        print("6. Xóa tài khoản")
        print("0. Quay lại")
        choice = input("Chọn (0-6): ").strip()
        if choice == '1':
            account_list_menu(profile)
        elif choice == '2':
            add_account_menu(profile_id)
        elif choice == '3':
            toggle_account_selection(profile)
        elif choice == '4':
            relogin_account(profile)
        elif choice == '5':
            edit_account(profile)
        elif choice == '6':
            delete_account(profile)
        elif choice == '0':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


def run_config_summary(config):
    jobs = []
    if 'like_facebook' in config['job_types']:
        jobs.append('Like')
    if 'like_poster' in config['job_types']:
        jobs.append('Comment')
    return (
        f"Job={'+'.join(jobs)} | max={config['max_jobs_per_acc']} | "
        f"burst L {config['like_batch_min']}-{config['like_batch_max']}, "
        f"C {config['comment_batch_min']}-{config['comment_batch_max']} | "
        f"delay L {config['like_delay_min']}-{config['like_delay_max']}s, "
        f"C {config['comment_delay_min']}-{config['comment_delay_max']}s, "
        f"fail {config['failed_delay_min']}-{config['failed_delay_max']}s, "
        f"acc {config['account_delay_min']}-{config['account_delay_max']}s | "
        f"continuous={'ON' if config['continuous'] else 'OFF'} | "
        f"hidden-comment-skip={'ON' if config['skip_hidden_comments'] else 'OFF'}"
    )


_RUN_WIZARD_CANCEL = object()


def _is_run_wizard_cancel(raw):
    return str(raw or "").strip().lower() in {"cancel", "q", "quit"}


def _choose_run_job_type():
    print(f"\n{Fore.YELLOW}=== CHỌN LOẠI JOB ==={Style.RESET_ALL}")
    print("1. Like")
    print("2. Comment")
    print("3. Like + Comment")
    print("0. Quay lại")
    choices = {"1": "like", "2": "comment", "3": "both"}
    while True:
        choice = input("Chọn (0-3): ").strip()
        if choice == "0" or _is_run_wizard_cancel(choice):
            return None
        if choice in choices:
            return choices[choice]
        print_error("Lựa chọn không hợp lệ. Vui lòng chọn 0, 1, 2 hoặc 3.")


def _prompt_run_wizard_int(label, current, minimum):
    while True:
        raw = input(f"{label} [{current}]: ").strip()
        if _is_run_wizard_cancel(raw):
            return _RUN_WIZARD_CANCEL
        if not raw:
            return current, False
        try:
            value = int(raw)
            if value < minimum:
                raise ValueError
            return value, value != current
        except ValueError:
            print_error(f"{label} phải là số nguyên >= {minimum}.")


def _prompt_run_wizard_range(label, current_min, current_max, minimum):
    mixed = current_min is None and current_max is None
    while True:
        default_min = "riêng theo loại job" if mixed else current_min
        default_max = "riêng theo loại job" if mixed else current_max
        raw_min = input(f"{label} min [{default_min}]: ").strip()
        if _is_run_wizard_cancel(raw_min):
            return _RUN_WIZARD_CANCEL
        raw_max = input(f"{label} max [{default_max}]: ").strip()
        if _is_run_wizard_cancel(raw_max):
            return _RUN_WIZARD_CANCEL

        if not raw_min and not raw_max:
            return (current_min, current_max), False
        if mixed and (not raw_min or not raw_max):
            print_error(
                f"{label} đang dùng riêng theo loại job; hãy nhập cả min và max "
                "hoặc Enter cả hai để giữ nguyên."
            )
            continue
        try:
            minimum_value = current_min if not raw_min else int(raw_min)
            maximum_value = current_max if not raw_max else int(raw_max)
            validated = validate_integer_range(
                minimum_value, maximum_value, label, minimum
            )
            return validated, validated != (current_min, current_max)
        except (TypeError, ValueError) as exc:
            print_error(str(exc) or f"{label} không hợp lệ.")


def _prompt_run_wizard_bool(label, current):
    default = "Y/n" if current else "y/N"
    while True:
        raw = input(f"{label} [{default}]: ").strip().lower()
        if _is_run_wizard_cancel(raw):
            return _RUN_WIZARD_CANCEL
        if not raw:
            return current, False
        if raw in {"y", "yes", "c", "co", "có"}:
            return True, current is not True
        if raw in {"n", "no", "k", "khong", "không"}:
            return False, current is not False
        print_error("Vui lòng nhập y hoặc n; Enter để giữ nguyên.")


def run_wizard_summary(config):
    facade = get_run_config_facade(config)
    job_labels = {
        "like": "Like",
        "comment": "Comment",
        "both": "Like + Comment",
    }
    if facade["job_delay_min"] is None:
        job_delay = "riêng theo từng loại job"
    else:
        job_delay = f"{facade['job_delay_min']}-{facade['job_delay_max']}s"
    return (
        f"Job       : {job_labels[facade['job_type']]}\n"
        f"Max/acc   : {facade['max_jobs_per_acc']}\n"
        f"Job delay : {job_delay}\n"
        f"Acc delay : {facade['account_delay_min']}-{facade['account_delay_max']}s\n"
        f"Continuous: {'ON' if facade['continuous'] else 'OFF'}"
    )


def _collect_run_wizard_config(run_config):
    config = normalize_run_config(run_config)
    facade = get_run_config_facade(config)
    job_type = _choose_run_job_type()
    if job_type is None:
        return None

    print(f"\n{Fore.YELLOW}=== CẤU HÌNH PHIÊN CHẠY ==={Style.RESET_ALL}")
    print("Enter để giữ giá trị hiện tại; nhập 'cancel' để quay lại.")
    updates = {"job_type": job_type}

    max_jobs = _prompt_run_wizard_int(
        "Số job / account", facade["max_jobs_per_acc"], 1
    )
    if max_jobs is _RUN_WIZARD_CANCEL:
        return None
    if max_jobs[1]:
        updates["max_jobs_per_acc"] = max_jobs[0]

    if facade["job_delay_min"] is None:
        print_info("Delay job hiện tại: đang dùng riêng theo từng loại job.")
    job_delay = _prompt_run_wizard_range(
        "Delay job", facade["job_delay_min"], facade["job_delay_max"], 0
    )
    if job_delay is _RUN_WIZARD_CANCEL:
        return None
    if job_delay[1]:
        updates["job_delay_min"], updates["job_delay_max"] = job_delay[0]

    account_delay = _prompt_run_wizard_range(
        "Delay chuyển account",
        facade["account_delay_min"],
        facade["account_delay_max"],
        0,
    )
    if account_delay is _RUN_WIZARD_CANCEL:
        return None
    if account_delay[1]:
        updates["account_delay_min"], updates["account_delay_max"] = account_delay[0]

    continuous = _prompt_run_wizard_bool("Chạy liên tục", facade["continuous"])
    if continuous is _RUN_WIZARD_CANCEL:
        return None
    if continuous[1]:
        updates["continuous"] = continuous[0]

    try:
        candidate = apply_run_config_facade(config, updates)
    except ValueError as exc:
        print_error(str(exc))
        return None

    print(f"\n{Fore.YELLOW}=== XÁC NHẬN PHIÊN CHẠY ==={Style.RESET_ALL}")
    print(run_wizard_summary(candidate))
    while True:
        confirmation = input("Chạy với cấu hình này? (y/n, 0 để quay lại): ").strip().lower()
        if confirmation in {"y", "yes", "c", "co", "có"}:
            return candidate, updates
        if confirmation in {"", "0", "n", "no", "k", "khong", "không"} or _is_run_wizard_cancel(confirmation):
            return None
        print_error("Vui lòng nhập y để chạy hoặc n/0 để quay lại.")


def configure_run(profile):
    result = _collect_run_wizard_config(profile.get("run_config"))
    if result is None:
        print_info("Đã hủy phiên chạy.")
        return None

    candidate, facade_updates = result
    persisted_updates = {"job_types": candidate["job_types"]}
    direct_fields = (
        "max_jobs_per_acc",
        "account_delay_min",
        "account_delay_max",
        "continuous",
    )
    for field in direct_fields:
        if field in facade_updates:
            persisted_updates[field] = candidate[field]
    if "job_delay_min" in facade_updates:
        persisted_updates.update({
            "like_delay_min": candidate["like_delay_min"],
            "like_delay_max": candidate["like_delay_max"],
            "comment_delay_min": candidate["comment_delay_min"],
            "comment_delay_max": candidate["comment_delay_max"],
        })

    try:
        config = update_profile_run_config(
            profile.get("profile_id"), persisted_updates
        )
        print_success("Đã lưu cấu hình phiên chạy.")
        return config
    except (KeyError, RuntimeError, ValueError) as exc:
        print_error(str(exc))
        return None


def list_profiles(data):
    profiles = data.get('profiles', [])
    if not profiles:
        print_info("Chưa có TTC profile.")
        return
    active = SESSION_PROFILE_ID if SESSION_PROFILE_ID is not None else data.get('active_profile_id')
    for index, profile in enumerate(profiles, start=1):
        mark = "▶️ " if profile['profile_id'] == active else "   "
        fb_count = len(profile.get('fb_accounts', []))
        print(f"{index}. {mark}{profile['name']} | FB: {fb_count}")


def _profile_index(data, prompt="Chọn profile để chạy: "):
    if not data.get('profiles'):
        print_warning("Chưa có TTC profile.")
        return None
    list_profiles(data)
    print()
    raw = input(prompt).strip()
    try:
        index = int(raw) - 1
    except ValueError:
        print_error("Index không hợp lệ.")
        return None
    if index < 0 or index >= len(data['profiles']):
        print_error("Index không hợp lệ.")
        return None
    return index


def create_profile(data):
    name = input("Nhập tên profile: ").strip()
    if not name:
        print_error("Tên không được trống.")
        return None
    token = input("Nhập TTC token: ").strip()

    print(f"\n{Fore.YELLOW}=== XÁC NHẬN PROFILE ==={Style.RESET_ALL}")
    print(f"Tên:\n{name}")
    print(f"\nTTC token:\n{token}")
    if input("\nTạo profile này? (y/n): ").strip().lower() not in ['y', 'yes']:
        print_info("Đã hủy.")
        return None

    try:
        profile = create_profile_record(name, token)
    except Exception as exc:
        print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")
        return None

    # Refresh only after commit; never append the new profile to a stale snapshot.
    latest = load_database()
    data.clear()
    data.update(latest)

    global SESSION_PROFILE_ID
    SESSION_PROFILE_ID = profile['profile_id']

    print_success(f"Đã tạo profile: {name}")

    if len(data['profiles']) > 1:
        if input("\nMuốn sao chép Facebook từ profile khác ngay bây giờ? (y/n): ").strip().lower() in ['y', 'yes']:
            copy_facebook_accounts_from_profile(SESSION_PROFILE_ID)

    return profile


def view_ttc_token(data):
    index = _profile_index(data, "Chọn profile muốn xem/cập nhật token: ")
    if index is None:
        return
    profile = data['profiles'][index]
    profile_id = profile['profile_id']
    profile_name = profile['name']
    while True:
        print(f"\n{Fore.YELLOW}=== TTC TOKEN — {profile['name']} ==={Style.RESET_ALL}")
        print("Token hiện tại:\n")
        print(profile.get('auth_token', ''))
        print("\n1. Giữ nguyên")
        print("2. Nhập token mới")
        print("3. Quay lại")
        choice = input("Chọn (1-3): ").strip()
        if choice == '1':
            return
        elif choice == '2':
            token = input("\nNhập TTC token mới: ").strip()
            print("\nToken vừa nhập:\n")
            print(token)
            if input("\nLưu token này? (y/n): ").strip().lower() in ['y', 'yes']:
                try:
                    update_profile_auth_token_manual(profile_id, token)
                    print_success("Đã cập nhật TTC token.")
                except ProfileInUseError:
                    print_error(
                        f'Không thể cập nhật token. Profile "{profile_name}" '
                        'đang được sử dụng ở phiên khác.'
                    )
                except Exception as exc:
                    print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")
                return
            else:
                print_info("Đã hủy.")
        elif choice == '3':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


def copy_facebook_accounts_from_profile(destination_profile_id):
    data = load_database()
    dest_id = destination_profile_id
    dest_profile = get_profile(data, dest_id)
    if not dest_profile:
        print_error("Profile đích của tab này không còn tồn tại.")
        return

    print(f"\n{Fore.YELLOW}=== SAO CHÉP FACEBOOK ==={Style.RESET_ALL}")
    print("Profile đích:")
    print(f"▶️ {dest_profile['name']}\n")
    print("Chọn profile nguồn:\n")

    profiles = data.get('profiles', [])
    source_profiles = [p for p in profiles if p['profile_id'] != dest_id]
    if not source_profiles:
        print_info("Không có profile nào khác để làm nguồn.")
        return

    for index, p in enumerate(source_profiles, start=1):
        fb_count = len(p.get('fb_accounts', []))
        print(f"{index}. {p['name']} | FB: {fb_count}")
    print("\n0. Hủy")

    raw = input("\nChọn: ").strip()
    if raw == '0':
        return
    try:
        idx = int(raw) - 1
        if idx < 0 or idx >= len(source_profiles):
            raise ValueError
        source = source_profiles[idx]
    except ValueError:
        print_error("Lựa chọn không hợp lệ.")
        return

    accounts = source.get('fb_accounts', [])
    if not accounts:
        print_info("Profile nguồn không có Facebook account.")
        return

    selected_keys = set(selected_account_keys(source))
    while True:
        print(f"\n{Fore.YELLOW}=== FACEBOOK TỪ {source['name']} ==={Style.RESET_ALL}")
        print(f"Profile nguồn:\n{source['name']}")
        print(f"Profile đích:\n{dest_profile['name']}\n")

        for i, acc in enumerate(accounts, start=1):
            key = facebook_account_key(acc)
            run_mark = "✅" if key in selected_keys else "❌"
            identity = str(acc.get('email', '')).strip() or "Manual token"
            token_view = preview_token(acc.get('token', ''))
            has_2fa = "Có" if str(acc.get('secret', '')).strip() else "Không"
            print(f"{i}. {run_mark} {identity} | token {token_view} | 2FA: {has_2fa}")

        print("\nA. Sao chép tất cả")
        print("S. Chọn account muốn sao chép")
        print("0. Hủy")

        action = input("\nChọn: ").strip().upper()
        if action == '0':
            return

        indices_to_copy = []
        if action == 'A' or action == 'ALL':
            indices_to_copy = list(range(len(accounts)))
        elif action == 'S':
            raw_sel = input("Nhập account muốn sao chép, ví dụ: 1,3,4: ").strip()
            sel = parse_account_selection(raw_sel, len(accounts))
            if sel == -1:
                print_error("Danh sách account không hợp lệ.")
                continue
            elif sel is None:
                return
            indices_to_copy = sel
        else:
            print_error("Lựa chọn không hợp lệ.")
            continue

        copied_count = [0]
        skipped_count = [0]
        source_profile_id = source['profile_id']
        dest_profile_id = dest_profile['profile_id']
        account_keys_to_copy = [facebook_account_key(accounts[idx]) for idx in indices_to_copy]

        def _mutator(d):
            prof_src = get_profile(d, source_profile_id)
            prof_dest = get_profile(d, dest_profile_id)
            if not prof_src or not prof_dest:
                raise ValueError("Không tìm thấy profile nguồn hoặc đích")

            src_accounts = prof_src.get('fb_accounts', [])
            dest_accounts = prof_dest.setdefault('fb_accounts', [])
            latest_by_key = {
                facebook_account_key(account): account
                for account in src_accounts
                if facebook_account_key(account)
            }

            for account_key in account_keys_to_copy:
                if not account_key or account_key not in latest_by_key:
                    raise ValueError(
                        "Facebook account nguồn đã thay đổi. Vui lòng mở lại danh sách và chọn lại."
                    )
                acc_to_copy = dict(latest_by_key[account_key])
                if _is_duplicate_account(dest_accounts, acc_to_copy):
                    skipped_count[0] += 1
                else:
                    dest_accounts.append(acc_to_copy)
                    _include_new_account_in_explicit_selection(prof_dest, acc_to_copy)
                    copied_count[0] += 1
            return True

        try:
            with profile_run_lock(dest_profile_id):
                mutate_database(_mutator)
                print_success(f"Đã sao chép {copied_count[0]} Facebook account.")
                print_info(f"Bỏ qua {skipped_count[0]} account đã tồn tại.")
        except RuntimeError as e:
            print_error(str(e))
        except Exception as e:
            print_error(f"Không thể lưu thay đổi.\nDữ liệu cũ được giữ nguyên. {e}")
        return


def select_profile_menu(data):
    global SESSION_PROFILE_ID
    print(f"\n{Fore.YELLOW}=== CHỌN PROFILE ĐỂ CHẠY ==={Style.RESET_ALL}")
    index = _profile_index(data)
    if index is None:
        return
    selected_id = data['profiles'][index]['profile_id']
    selected_name = data['profiles'][index]['name']
    try:
        select_profile_as_default(selected_id)
        SESSION_PROFILE_ID = selected_id
        print_success(f"Đã chọn profile để chạy: {selected_name}")
    except Exception as exc:
        print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")


def rename_profile_menu(data):
    print(f"\n{Fore.YELLOW}=== ĐỔI TÊN PROFILE ==={Style.RESET_ALL}")
    index = _profile_index(data, "Chọn profile muốn đổi tên: ")
    if index is None:
        return
    profile_id = data['profiles'][index]['profile_id']
    old_name = data['profiles'][index]['name']
    name = input("Tên mới: ").strip()
    if not name:
        return
    try:
        rename_profile(profile_id, name)
        print_success("Đã đổi tên.")
    except ProfileInUseError:
        print_error(
            f'Không thể đổi tên. Profile "{old_name}" '
            'đang được sử dụng ở phiên khác.'
        )
    except Exception as exc:
        print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")


def delete_profile_menu(data):
    global SESSION_PROFILE_ID
    print(f"\n{Fore.YELLOW}=== XÓA PROFILE ==={Style.RESET_ALL}")
    index = _profile_index(data, "Chọn profile muốn xóa: ")
    if index is None:
        return
    profile = data['profiles'][index]
    fb_count = len(profile.get('fb_accounts', []))
    print("\n=== XÁC NHẬN XÓA PROFILE ===")
    print(f"Profile:\n{profile['name']}")
    print(f"\nFacebook:\n{fb_count} account")
    print(f"\nTTC token:\n{preview_token(profile.get('auth_token'))}")
    if input("\nXóa profile này? (y/n): ").strip().lower() not in ['y', 'yes']:
        return
    deleted_id = profile['profile_id']
    deleted_name = profile['name']
    try:
        _, new_active_id = delete_profile(deleted_id)
        if SESSION_PROFILE_ID == deleted_id:
            SESSION_PROFILE_ID = new_active_id
        print_success("Đã xóa TTC profile.")
    except ProfileInUseError:
        print_error(
            f'Không thể xóa. Profile "{deleted_name}" '
            'đang được sử dụng ở phiên khác.'
        )
    except Exception as exc:
        print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")


def profile_manager(data):
    while True:
        try:
            latest = load_database()
            data.clear()
            data.update(latest)
        except Exception as exc:
            print_error(f"Không thể đọc dữ liệu mới nhất. {exc}")
            return
        print(f"\n{Fore.YELLOW}=== QUẢN LÝ TTC PROFILE ==={Style.RESET_ALL}\n")
        active = SESSION_PROFILE_ID or data.get('active_profile_id')
        active_name = "CHƯA CHỌN"
        for p in data.get('profiles', []):
            if p['profile_id'] == active:
                active_name = p['name']
                break
        print("Profile của tab này:")
        print(f"▶️ {active_name}\n")
        print("1. Đổi tên profile")
        print("2. Sao chép Facebook từ profile khác")
        print("3. Xóa profile")
        print("0. Quay lại")
        choice = input("Chọn (0-3): ").strip()
        if choice == '1':
            rename_profile_menu(data)
        elif choice == '2':
            copy_facebook_accounts_from_profile(SESSION_PROFILE_ID)
        elif choice == '3':
            delete_profile_menu(data)
        elif choice == '0':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


# ---------- Chạy đa Facebook account ----------
def _ttc_no_job_wait_seconds(no_job_metadata):
    for item in no_job_metadata:
        if not isinstance(item, dict):
            raise ValueError("TTC adapter returned invalid no-job metadata")
        raw_countdown = item.get("countdown")
        if isinstance(raw_countdown, bool):
            continue
        if isinstance(raw_countdown, int):
            seconds = raw_countdown
        elif isinstance(raw_countdown, str) and re.fullmatch(r"[+-]?\d+", raw_countdown.strip()):
            seconds = int(raw_countdown.strip())
        else:
            continue
        # Server countdowns remain endpoint metadata. The runner only uses a
        # short cooperative tick so one endpoint cannot globally block others.
        if seconds < 0:
            continue
    return TTC_ACQUISITION_POLL_SECONDS


def _normalize_ttc_earned_coin(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return max(0, int(value.strip()))
    return 0


def _latest_known_ttc_balance(api, claim_result=None):
    if isinstance(claim_result, dict):
        authoritative = _parse_strict_integer(claim_result.get("balance"))
        if authoritative is not None:
            return authoritative
    get_account_info = getattr(api, "get_account_info", None)
    if not callable(get_account_info):
        return None
    account_info = get_account_info()
    if not isinstance(account_info, dict):
        return None
    return _parse_strict_integer(account_info.get("balance"))


def _runtime_stop_requested(stop_event):
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(stop_event):
        return bool(stop_event())
    raise TypeError("stop_event must expose is_set() or be callable")


def _emit_runtime_event(event_callback, event_type, message="", **payload):
    if event_callback is None:
        return
    event = {
        "type": str(event_type),
        "message": _mask_sensitive_text(message),
    }
    event.update({
        key: _mask_sensitive_text(value) if isinstance(value, str) else value
        for key, value in payload.items()
    })
    event_callback(event)


def _runtime_log(event_callback, level, message, terminal_printer=print_info,
                 visibility="debug", **payload):
    """Render USER/CRITICAL output normally and retain DEBUG diagnostics."""
    visibility = str(visibility).strip().casefold()
    if visibility not in {"user", "critical", "debug"}:
        raise ValueError(f"Unsupported runtime log visibility: {visibility!r}")
    if visibility == "debug" and not _runtime_debug_enabled():
        return
    safe_message = _mask_sensitive_text(message)
    terminal_printer(safe_message)
    _emit_runtime_event(
        event_callback,
        "runtime_log",
        safe_message,
        level=str(level),
        visibility=visibility,
        **payload,
    )


def _runtime_server_response(event_callback, job_type, server_raw_text,
                             **payload):
    """Publish masked TTC evidence only through the explicit debug boundary."""
    if not _runtime_debug_enabled():
        return
    print_ttc_server_response(job_type, server_raw_text)
    safe_raw_text = _mask_sensitive_text(server_raw_text)
    _emit_runtime_event(
        event_callback,
        "runtime_log",
        f"[TTC][{job_type}]\n{safe_raw_text}",
        level="raw",
        job_type=str(job_type),
        server_raw_text=safe_raw_text,
        **payload,
    )


def _runtime_job_result(event_callback, index, total, job_type, state,
                        earned_coin=None, type_job_source=None, **payload):
    """Render one table row from an already-decided actual job state."""
    labels = {
        "success": ("Thành công", "job_succeeded", "success"),
        "failed": ("Thất bại", "job_failed", "error"),
        "skipped": ("Bỏ qua", "job_skipped", "warning"),
    }
    if state not in labels:
        raise ValueError(f"Unsupported job result state: {state!r}")
    label, event_type, level = labels[state]
    success = state == "success"
    print_job_row(
        index,
        payload.get("uid"),
        payload.get("post_id"),
        compact_ttc_job_type(type_job_source),
        earned_coin if success and earned_coin is not None else 0,
        payload.get("balance"),
        success,
    )
    line = f"{str(job_type).upper()} | {label}"
    if success and earned_coin is not None:
        line += f" | +{format_coin(earned_coin)} xu"
    event_payload = {
        "level": level,
        "index": index,
        "job_type": str(job_type),
        "result_state": state,
        "earned_coin": earned_coin,
        **payload,
    }
    if total is not None:
        event_payload["total"] = total
    _emit_runtime_event(event_callback, event_type, line, **event_payload)


def _cooperative_countdown(seconds, message, stop_event=None,
                           event_callback=None):
    seconds = max(0, int(seconds))
    wait = getattr(stop_event, "wait", None)
    for remaining in range(seconds, 0, -1):
        _emit_runtime_event(
            event_callback,
            "delay",
            message,
            remaining=remaining,
            seconds=seconds,
        )
        if event_callback is None:
            print(
                f"\r{Fore.YELLOW}⏳ {message} {remaining}s...{Style.RESET_ALL}",
                end="",
                flush=True,
            )
        if callable(wait):
            if wait(1):
                if event_callback is None:
                    print("\r\033[2K", end="", flush=True)
                return False
            continue
        if _runtime_stop_requested(stop_event):
            if event_callback is None:
                print("\r\033[2K", end="", flush=True)
            return False
        time.sleep(1)
    if seconds and event_callback is None:
        print("\r\033[2K", end="", flush=True)
    return not _runtime_stop_requested(stop_event)


def run_ttc_jobs(api, fb_token, max_jobs=30, job_types=None,
                 like_batch_limit=None, comment_batch_limit=None,
                 like_batch_min=None, like_batch_max=None,
                 comment_batch_min=None, comment_batch_max=None,
                 like_delay_range=(10, 10), comment_delay_range=(10, 10),
                 failed_delay_range=(10, 10), skip_hidden_comments=True,
                 stop_event=None, event_callback=None, metrics=None,
                 action=None, transaction_sleep_fn=None,
                 configure_remote_uid=True, facebook_live_result=None):
    global total_coin
    enabled_types = normalize_job_types(job_types)
    transaction_sleep_fn = transaction_sleep_fn or time.sleep
    if metrics is not None:
        metrics.setdefault("jobs_succeeded", 0)
        metrics.setdefault("session_coin", 0)
        metrics.setdefault("balance", None)
    if _runtime_stop_requested(stop_event):
        _emit_runtime_event(event_callback, "stopped", "Đã dừng trước khi bắt đầu job.")
        return 0
    do_job = action if action is not None else AUTO_Action()

    def action_log(message, level="info"):
        printers = {
            "success": print_success,
            "warning": print_warning,
            "error": print_error,
            "raw": print,
        }
        _runtime_log(
            event_callback,
            level,
            message,
            terminal_printer=printers.get(level, print_info),
            stage="facebook_action",
        )

    set_action_log = getattr(do_job, "set_log_fn", None)
    if callable(set_action_log):
        set_action_log(action_log)
    preflight_performed = facebook_live_result is None
    if not preflight_performed:
        live_result = _normalize_facebook_live_result(facebook_live_result)
    else:
        check_live = getattr(do_job, "check_live", None)
        if callable(check_live):
            live_result = _normalize_facebook_live_result(check_live(fb_token))
        else:
            # Compatibility for injected legacy actions. A missing UID is
            # ambiguous, so it must never be promoted to DIE_TOKEN.
            uid, message = do_job.get_info(fb_token)
            live_result = _normalize_facebook_live_result({
                "status": FACEBOOK_LIVE if isinstance(uid, str) and uid.strip()
                else FACEBOOK_CHECK_ERROR,
                "uid": uid,
                "reason": "Facebook token LIVE" if isinstance(uid, str) and uid.strip()
                else message,
            })
        account_live_message = (
            f"Facebook token/account không dùng được: {live_result.reason}"
            if live_result.status == FACEBOOK_DIE_TOKEN
            else f"Không thể kiểm tra Facebook account: {live_result.reason}"
            if live_result.status != FACEBOOK_LIVE
            else "Facebook token LIVE"
            if _runtime_debug_enabled()
            else ""
        )
        account_live_message = _mask_sensitive_text(account_live_message)
        if live_result.status != FACEBOOK_LIVE:
            print_error(account_live_message)
        _emit_runtime_event(
            event_callback,
            "account_live",
            account_live_message,
            status=live_result.status,
            uid=live_result.uid,
            reason=live_result.reason,
            http_status=live_result.http_status,
        )
    if live_result.status != FACEBOOK_LIVE:
        return 0
    uid = live_result.uid

    if configure_remote_uid:
        api.set_facebook_uid(uid)
    else:
        api.bind_facebook_uid(uid)
    if _runtime_debug_enabled():
        print(f"{Fore.CYAN}{Style.BRIGHT}📱 Facebook: {uid}{Style.RESET_ALL}")
    _emit_runtime_event(
        event_callback,
        "identity",
        f"Facebook UID: {uid}" if _runtime_debug_enabled() else "",
        uid=uid,
    )

    total_done = 0
    processed_jobs = 0
    comment_disabled_for_this_turn = False
    active_types = set(enabled_types)
    job_queue = []
    attempted_job_keys = set()
    latest_balance = (
        metrics.get("balance") if metrics is not None
        else _latest_known_ttc_balance(api)
    )

    def wait_after_actual_job(succeeded, actual_type):
        if total_done >= max_jobs or not active_types:
            return True
        if _runtime_stop_requested(stop_event):
            return False
        delay_range = (
            like_delay_range if succeeded and actual_type == "like_facebook"
            else comment_delay_range if succeeded
            else failed_delay_range
        )
        delay = random.randint(*delay_range)
        return _cooperative_countdown(
            delay, "Chờ job tiếp theo", stop_event, event_callback
        )

    while total_done < max_jobs and active_types:
        if _runtime_stop_requested(stop_event):
            _emit_runtime_event(
                event_callback, "stopped", "Đã dừng trước job tiếp theo.",
                jobs_succeeded=total_done,
            )
            break
        if not job_queue:
            actual_jobs = []
            no_job_metadata = []
            for fetch_type in ("like_facebook", "like_poster"):
                if fetch_type not in active_types:
                    continue
                result = api.fetch_jobs_for_legacy_type(fetch_type)
                if (not isinstance(result, dict)
                        or not isinstance(result.get("jobs"), list)
                        or not isinstance(result.get("no_job"), list)):
                    raise ValueError("TTC adapter returned an invalid fetch result")
                cooldowns = result.get("cooldowns", [])
                permanent_skips = result.get("permanent_skips", [])
                if (not isinstance(cooldowns, list)
                        or not isinstance(permanent_skips, list)):
                    raise ValueError("TTC adapter returned invalid endpoint-state metadata")

                for item in cooldowns:
                    if not isinstance(item, dict):
                        raise ValueError("TTC adapter returned invalid cooldown metadata")
                    if item.get("source") == "server":
                        _runtime_server_response(
                            event_callback,
                            item.get("job_type", ""),
                            item.get("server_raw_text", ""),
                            stage="fetch_cooldown",
                        )
                        _runtime_log(
                            event_callback,
                            "info",
                            f"Tạm bỏ qua endpoint này trong "
                            f"{item.get('retry_after', 0)} giây chờ TTC.",
                            job_type=item.get("job_type", ""),
                            retry_after=item.get("retry_after", 0),
                        )
                    _emit_runtime_event(
                        event_callback,
                        "endpoint_cooldown",
                        "TTC endpoint cooldown",
                        level="warning",
                        job_type=item.get("job_type", ""),
                        remaining=item.get("remaining", 0),
                        retry_at=item.get("retry_at"),
                        source=item.get("source", ""),
                    )
                    no_job_metadata.append({
                        "countdown": item.get("remaining", 0),
                    })

                for item in permanent_skips:
                    if not isinstance(item, dict):
                        raise ValueError(
                            "TTC adapter returned invalid permanent-skip metadata"
                        )
                    if item.get("source") == "server":
                        _runtime_server_response(
                            event_callback,
                            item.get("job_type", ""),
                            item.get("server_raw_text", ""),
                            stage="vip_ineligible",
                        )
                        _runtime_log(
                            event_callback,
                            "warning",
                            "Bỏ qua endpoint VIP không đủ điều kiện.",
                            job_type=item.get("job_type", ""),
                            reason="vip_ineligible",
                        )

                no_job_metadata.extend(result["no_job"])
                actual_jobs.extend(result["jobs"])
                if (
                    not result["jobs"]
                    and not result["no_job"]
                    and not cooldowns
                    and permanent_skips
                ):
                    active_types.discard(fetch_type)

            unique_jobs = []
            queued_keys = set()
            for candidate in actual_jobs:
                original = candidate.get("_ttc_job") if isinstance(candidate, dict) else None
                if isinstance(original, dict):
                    job_key = str(
                        original.get("job_id")
                        or original.get("idpost")
                        or original.get("object_id")
                        or ""
                    )
                else:
                    job_key = ""
                if job_key and (job_key in attempted_job_keys or job_key in queued_keys):
                    _runtime_log(
                        event_callback, "info", "Bỏ qua job đã xử lý trong account run.",
                        job_id=job_key, reason="duplicate_actual_job",
                    )
                    continue
                if job_key:
                    queued_keys.add(job_key)
                unique_jobs.append(candidate)

            job_queue.extend(schedule_jobs_by_burst(
                unique_jobs,
                active_types,
                len(unique_jobs),
                like_batch_limit=like_batch_limit,
                comment_batch_limit=comment_batch_limit,
                like_batch_min=like_batch_min,
                like_batch_max=like_batch_max,
                comment_batch_min=comment_batch_min,
                comment_batch_max=comment_batch_max,
            ))
            if not job_queue:
                if not active_types:
                    break
                wait_seconds = _ttc_no_job_wait_seconds(no_job_metadata)
                _runtime_log(
                    event_callback,
                    "info",
                    "Không có actual TTC job; chờ polling tick.",
                    wait_seconds=wait_seconds,
                )
                if not _cooperative_countdown(
                        wait_seconds, "Chờ lấy job", stop_event, event_callback):
                    _emit_runtime_event(
                        event_callback, "stopped", "Đã ngắt chờ lấy job.",
                        jobs_succeeded=total_done,
                    )
                    break
                continue

        if _runtime_stop_requested(stop_event):
            _emit_runtime_event(
                event_callback, "stopped", "Đã dừng sau khi lấy job.",
                jobs_succeeded=total_done,
            )
            break
        job = job_queue.pop(0)
        type_ = job.get("type") if isinstance(job, dict) else ""
        processed_jobs += 1
        idx = processed_jobs
        total_jobs = None
        original_job = job.get("_ttc_job")
        if isinstance(original_job, dict):
            attempted_key = str(
                original_job.get("job_id")
                or original_job.get("idpost")
                or original_job.get("object_id")
                or ""
            )
            if attempted_key:
                attempted_job_keys.add(attempted_key)
        job_succeeded = False
        if not isinstance(original_job, dict):
            _runtime_job_result(
                event_callback, idx, total_jobs, type_ or "UNKNOWN", "failed",
                uid=uid, post_id="-", job_id="-", balance=latest_balance,
            )
            _runtime_log(
                event_callback, "error",
                "TTC scheduler wrapper thiếu actual job; bỏ qua.",
                stage="job_validation",
            )
            if not wait_after_actual_job(False, type_):
                break
            continue
        else:
            object_id = original_job.get("object_id")
            if not object_id:
                _runtime_job_result(
                    event_callback, idx, total_jobs,
                    "LIKE" if type_ == "like_facebook" else "COMMENT",
                    "failed",
                    uid=uid,
                    post_id="-",
                    job_id=original_job.get("job_id", ""),
                    balance=latest_balance,
                    type_job_source=original_job.get("ttc_source"),
                )
                _runtime_log(
                    event_callback, "warning",
                    "Job TTC thiếu object_id; bỏ qua.",
                    terminal_printer=print_warning,
                    job_id=original_job.get("job_id", ""),
                )
                if not wait_after_actual_job(False, type_):
                    break
                continue
            else:
                job_type_display = "LIKE" if type_ == "like_facebook" else "COMMENT"
                if type_ == "like_facebook":
                    reaction = original_job.get("reaction_type")
                    if not reaction:
                        _runtime_job_result(
                            event_callback, idx, total_jobs,
                            job_type_display, "failed", uid=uid, post_id="-",
                            job_id=original_job.get("job_id", ""),
                            balance=latest_balance,
                            type_job_source=original_job.get("ttc_source"),
                        )
                        _runtime_log(
                            event_callback, "error",
                            "Job TTC thiếu reaction_type; bỏ qua.",
                            stage="job_validation",
                        )
                        if not wait_after_actual_job(False, type_):
                            break
                        continue
                else:
                    comment_text = original_job.get("comment_text")
                    if not isinstance(comment_text, str) or not comment_text:
                        _runtime_job_result(
                            event_callback, idx, total_jobs,
                            job_type_display, "failed", uid=uid, post_id="-",
                            job_id=original_job.get("job_id", ""),
                            balance=latest_balance,
                            type_job_source=original_job.get("ttc_source"),
                        )
                        _runtime_log(
                            event_callback, "error",
                            "Job TTC thiếu comment_text; bỏ qua.",
                            stage="job_validation",
                        )
                        if not wait_after_actual_job(False, type_):
                            break
                        continue
                _runtime_log(
                    event_callback,
                    "info",
                    f"[{idx}] Đang xử lý {job_type_display} "
                    f"(job_id: {original_job.get('job_id', '')})...",
                    job_id=original_job.get("job_id", ""),
                )
                _emit_runtime_event(
                    event_callback,
                    "job_started",
                    (
                        f"[{idx}] Đang xử lý {job_type_display} "
                        f"(job_id: {original_job.get('job_id', '')}) | "
                        f"object: {object_id}"
                        if _runtime_debug_enabled() else ""
                    ),
                    index=idx,
                    job_type=type_,
                    uid=uid,
                    job_id=original_job.get("job_id", ""),
                    object_id=object_id,
                )
                action_succeeded = False
                action_failure_decision_logged = False
                post_id = "-"
                try:
                    post_id = _resolve_ttc_facebook_post_id(
                        original_job,
                        log_fn=action_log,
                    )
                    if post_id is not None and int(post_id) > 0:
                        if type_ == "like_facebook":
                            _runtime_log(
                                event_callback, "info",
                                f"Đang thực hiện {reaction} trên Facebook object {object_id}",
                                reaction_type=reaction,
                                object_id=object_id,
                                post_id=str(post_id),
                            )
                            action_succeeded = do_job.auto_reaction(
                                post_id, reaction, fb_token, uid
                            )
                            reaction_evidence = getattr(
                                do_job, "last_reaction_evidence", {}
                            )
                            if not isinstance(reaction_evidence, dict):
                                reaction_evidence = {}
                            reaction_attempts = reaction_evidence.get("attempts", 1)
                            if (
                                not isinstance(reaction_attempts, int)
                                or isinstance(reaction_attempts, bool)
                            ):
                                reaction_attempts = 1
                            if action_succeeded:
                                reaction_message = (
                                    f"{reaction} thành công sau 1 lần thử lại."
                                    if reaction_attempts == 2
                                    else f"{reaction} thành công."
                                )
                                reaction_level = "success"
                            elif reaction_attempts == 2:
                                reaction_message = (
                                    f"{reaction} vẫn thất bại sau 1 lần thử lại; "
                                    "bỏ qua job này."
                                )
                                reaction_level = "error"
                                action_failure_decision_logged = True
                            elif reaction_evidence.get("classification") == "NETWORK_UNKNOWN":
                                reaction_message = (
                                    f"{reaction} gặp lỗi mạng; kết quả phản ứng chưa xác định "
                                    "nên không thử lại; bỏ qua job này."
                                )
                                reaction_level = "error"
                                action_failure_decision_logged = True
                            elif reaction_evidence.get("classification") == "PARSE_ERROR":
                                reaction_message = (
                                    f"Không đọc được phản hồi Facebook cho {reaction}; "
                                    "không thử lại; bỏ qua job này."
                                )
                                reaction_level = "error"
                                action_failure_decision_logged = True
                            else:
                                reaction_message = (
                                    f"{reaction} thất bại; bỏ qua job này."
                                )
                                reaction_level = "error"
                                action_failure_decision_logged = True
                            _emit_runtime_event(
                                event_callback,
                                "facebook_action_result",
                                reaction_message if _runtime_debug_enabled() else "",
                                level=reaction_level,
                                action="reaction",
                                reaction_type=reaction,
                                object_id=object_id,
                                post_id=str(post_id),
                                success=bool(action_succeeded),
                                classification=reaction_evidence.get(
                                    "classification", ""
                                ),
                                attempts=reaction_attempts,
                            )
                        else:
                            _runtime_log(
                                event_callback, "info",
                                f"Đang thực hiện Comment trên Facebook object {object_id}",
                                object_id=object_id,
                                post_id=str(post_id),
                            )
                            action_succeeded = do_job.auto_cmt(
                                post_id, comment_text, fb_token, uid
                            )
                            _emit_runtime_event(
                                event_callback,
                                "facebook_action_result",
                                (
                                    "Comment thành công."
                                    if action_succeeded
                                    else "Comment thất bại; bỏ qua job này."
                                ) if _runtime_debug_enabled() else "",
                                level="success" if action_succeeded else "error",
                                action="comment",
                                object_id=object_id,
                                post_id=str(post_id),
                                success=bool(action_succeeded),
                            )
                            if not action_succeeded:
                                action_failure_decision_logged = True
                            comment_evidence = getattr(do_job, "last_comment_evidence", None)
                            if not isinstance(comment_evidence, dict):
                                comment_evidence = {}
                            if action_succeeded:
                                if _is_comment_pending_approval(comment_evidence):
                                    _runtime_log(
                                        event_callback, "warning",
                                        "Acc yếu bị Facebook ẩn comment.",
                                        terminal_printer=print_warning,
                                        comment_state="pending_approval",
                                    )
                                    if skip_hidden_comments:
                                        comment_disabled_for_this_turn = True
                                        _runtime_log(
                                            event_callback, "info",
                                            "Bỏ qua TTC claim cho Comment này.",
                                            reason="pending_approval",
                                        )
                                        _runtime_log(
                                            event_callback, "info",
                                            "Bỏ Comment trong lượt hiện tại, "
                                            "chuyển sang Like tiếp tục làm.",
                                            reason="pending_approval",
                                        )
                        if not comment_disabled_for_this_turn:
                            settle_seconds = (
                                5 if type_ == "like_poster" and action_succeeded else 3
                            )
                            transaction_sleep_fn(settle_seconds)
                    else:
                        _runtime_log(
                            event_callback, "warning",
                            f"Không lấy được Facebook ID cho "
                            f"{original_job.get('job_id', '')}",
                            terminal_printer=print_warning,
                            job_id=original_job.get("job_id", ""),
                        )
                except FacebookInteractionBlocked:
                    raise
                except Exception as exc:
                    _runtime_log(
                        event_callback, "error",
                        f"[{idx}] Lỗi Facebook {type_}: {exc}",
                        terminal_printer=print_error,
                        stage="facebook_action",
                    )
                    _emit_runtime_event(
                        event_callback, "error",
                        f"Lỗi Facebook {type_}: {exc}"
                        if _runtime_debug_enabled() else "",
                        stage="facebook_action", uid=uid,
                    )

                if comment_disabled_for_this_turn:
                    active_types.discard("like_poster")
                    job_queue[:] = [
                        queued_job for queued_job in job_queue
                        if queued_job.get("type") != "like_poster"
                    ]
                    comment_disabled_for_this_turn = False
                    action_succeeded = False
                    action_failure_decision_logged = True
                if action_succeeded:
                    result = api.submit_job(original_job)
                    claim_retried = False
                    if (
                        type_ == "like_facebook"
                        and action_succeeded is True
                        and result.get("success") is not True
                        and result.get("reason") == "NOT_OBSERVED_YET"
                    ):
                        server_raw_text = result.get("server_raw_text", "")
                        if isinstance(server_raw_text, str) and server_raw_text:
                            _runtime_server_response(
                                event_callback, "CLAIM", server_raw_text,
                                stage="claim_retry",
                            )
                        _runtime_log(
                            event_callback, "warning",
                            "TTC chưa ghi nhận reaction, thử gửi lại kết quả "
                            f"sau {TTC_CLAIM_RETRY_DELAY} giây...",
                            terminal_printer=print_warning,
                            retry_attempt=1,
                            retry_after=TTC_CLAIM_RETRY_DELAY,
                        )
                        transaction_sleep_fn(TTC_CLAIM_RETRY_DELAY)
                        result = api.submit_job(original_job)
                        claim_retried = True
                    if result.get("success") is True:
                        earned = _normalize_ttc_earned_coin(result.get("earned_coin"))
                        job_succeeded = True
                        total_done += 1
                        if metrics is None:
                            total_coin += earned
                            displayed_total = total_coin
                        else:
                            metrics["jobs_succeeded"] += 1
                            metrics["session_coin"] += earned
                            displayed_total = metrics["session_coin"]
                        latest_balance = _latest_known_ttc_balance(api, result)
                        if metrics is not None:
                            metrics["balance"] = latest_balance
                        _runtime_job_result(
                            event_callback, idx, total_jobs,
                            job_type_display, "success",
                            earned_coin=earned,
                            uid=uid,
                            post_id=str(post_id),
                            job_id=original_job.get("job_id", ""),
                            jobs_succeeded=(
                                total_done if metrics is None
                                else metrics["jobs_succeeded"]
                            ),
                            session_coin=displayed_total,
                            balance=latest_balance,
                            claim_id=original_job.get("idpost", ""),
                            type_job_source=original_job.get("ttc_source"),
                        )
                    else:
                        server_raw_text = result.get("server_raw_text", "")
                        if isinstance(server_raw_text, str) and server_raw_text:
                            _runtime_server_response(
                                event_callback, "CLAIM", server_raw_text,
                                stage="claim_rejected",
                            )
                        rejection = result.get("message") or result.get("reason") or "unknown"
                        if claim_retried:
                            _runtime_log(
                                event_callback, "warning",
                                "TTC vẫn không xác nhận job sau 1 lần thử lại. "
                                f"Lý do: {rejection}",
                                terminal_printer=print_warning,
                                claim_reason=result.get("reason", ""),
                                retry_attempt=1,
                            )
                        else:
                            _runtime_log(
                                event_callback, "warning",
                                f"TTC không xác nhận job. Lý do: {rejection}",
                                terminal_printer=print_warning,
                                claim_reason=result.get("reason", ""),
                            )
                        _runtime_job_result(
                            event_callback, idx, total_jobs,
                            job_type_display, "failed",
                            uid=uid,
                            post_id=str(post_id),
                            job_id=original_job.get("job_id", ""),
                            balance=latest_balance,
                            claim_reason=result.get("reason", ""),
                            type_job_source=original_job.get("ttc_source"),
                        )
                elif not action_failure_decision_logged:
                    _runtime_job_result(
                        event_callback, idx, total_jobs,
                        job_type_display, "failed",
                        uid=uid,
                        post_id=str(post_id),
                        job_id=original_job.get("job_id", ""),
                        balance=latest_balance,
                        type_job_source=original_job.get("ttc_source"),
                    )
                    _runtime_log(
                        event_callback, "error",
                        f"{job_type_display} | Thao tác Facebook thất bại; bỏ qua job này.",
                        terminal_printer=print_error,
                        job_id=original_job.get("job_id", ""),
                    )
                else:
                    _runtime_job_result(
                        event_callback, idx, total_jobs,
                        job_type_display, "failed",
                        uid=uid,
                        post_id=str(post_id),
                        job_id=original_job.get("job_id", ""),
                        balance=latest_balance,
                        type_job_source=original_job.get("ttc_source"),
                    )

        if not wait_after_actual_job(job_succeeded, type_):
            _emit_runtime_event(
                event_callback, "stopped", "Đã ngắt thời gian chờ để dừng.",
                jobs_succeeded=total_done,
            )
            break

    return total_done


class TTCAccountRuntime:
    """Independent production runtime for one Facebook account.

    This object owns its TTC client/session, fetch state, Facebook action
    session, stop state, immutable run snapshot, and GUI-facing metrics.
    """

    def __init__(self, profile_id, auth_token, account, config, worker_id=None,
                 stop_event=None, event_callback=None, api_factory=None,
                 action_factory=None, transaction_sleep_fn=None,
                 fetch_state=None):
        self.profile_id = str(profile_id or "").strip()
        self.auth_token = str(auth_token or "").strip()
        self.account = _normalize_account(dict(account or {}))
        self.account_key = facebook_account_key(self.account)
        self.config = normalize_run_config(dict(config or {}))
        self.worker_id = str(worker_id or self.account_key or "account")
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.event_callback = event_callback
        self.api_factory = api_factory or API_TTC
        self.action_factory = action_factory or AUTO_Action
        self.transaction_sleep_fn = transaction_sleep_fn
        if fetch_state is not None and not isinstance(fetch_state, TTCFetchState):
            raise TypeError("fetch_state must be TTCFetchState")
        self.fetch_state = fetch_state if fetch_state is not None else TTCFetchState()
        self.metrics = {
            "jobs_succeeded": 0,
            "session_coin": 0,
            "balance": None,
        }
        self.api = None
        self.facebook_action = None
        self.uid = None

    def request_stop(self):
        self.stop_event.set()

    def _handle_event(self, event):
        if event.get("type") == "identity":
            self.uid = event.get("uid")
        if self.event_callback is not None:
            self.event_callback(dict(event))

    def _ttc_log(self, message, level="api"):
        if not _runtime_debug_enabled():
            return
        self._handle_event({
            "type": "log",
            "message": _mask_sensitive_text(message),
            "level": str(level),
            "visibility": "debug",
        })

    def run(self):
        if not self.auth_token:
            raise ValueError("TTC profile chưa có token.")
        if not self.account_key or not self.account.get("token"):
            raise ValueError("Facebook account không có identity/token hợp lệ.")
        if _runtime_stop_requested(self.stop_event):
            return dict(self.metrics)

        self.api = self.api_factory(
            self.auth_token,
            worker_id=self.worker_id,
            fetch_state=self.fetch_state,
            log_fn=self._ttc_log,
        )
        self.facebook_action = self.action_factory()
        try:
            self._handle_event({
                "type": "status",
                "message": "Đang đăng nhập TTC" if _runtime_debug_enabled() else "",
                "status": "Running",
            })
            account_info = self.api.login()
            if isinstance(account_info, dict):
                self.metrics["balance"] = _parse_strict_integer(
                    account_info.get("balance")
                )
                self._handle_event({
                    "type": "balance",
                    "balance": self.metrics["balance"],
                })

            while not _runtime_stop_requested(self.stop_event):
                run_ttc_jobs(
                    self.api,
                    self.account["token"],
                    max_jobs=self.config["max_jobs_per_acc"],
                    job_types=set(self.config["job_types"]),
                    like_batch_min=self.config["like_batch_min"],
                    like_batch_max=self.config["like_batch_max"],
                    comment_batch_min=self.config["comment_batch_min"],
                    comment_batch_max=self.config["comment_batch_max"],
                    like_delay_range=(
                        self.config["like_delay_min"],
                        self.config["like_delay_max"],
                    ),
                    comment_delay_range=(
                        self.config["comment_delay_min"],
                        self.config["comment_delay_max"],
                    ),
                    failed_delay_range=(
                        self.config["failed_delay_min"],
                        self.config["failed_delay_max"],
                    ),
                    skip_hidden_comments=self.config["skip_hidden_comments"],
                    stop_event=self.stop_event,
                    event_callback=self._handle_event,
                    metrics=self.metrics,
                    action=self.facebook_action,
                    transaction_sleep_fn=self.transaction_sleep_fn,
                )
                if not self.config["continuous"]:
                    break

            status = "Stopped" if _runtime_stop_requested(self.stop_event) else "Completed"
            self._handle_event({
                "type": "status",
                "message": (
                    "Đã dừng" if status == "Stopped" else "Hoàn thành"
                ) if _runtime_debug_enabled() else "",
                "status": status,
                **self.metrics,
            })
            return dict(self.metrics)
        except FacebookInteractionBlocked as exc:
            blocked_at = exc.blocked_at.isoformat()
            status = _interaction_blocked_status(exc.blocked_at)
            self.metrics["interaction_blocked_at"] = blocked_at
            self._handle_event({
                "type": INTERACTION_BLOCKED_EVENT,
                "message": status,
                "status": status,
                "classification": exc.classification,
                "blocked_at": blocked_at,
                "facebook_message": exc.response_message,
                **self.metrics,
            })
            return dict(self.metrics)
        except Exception as exc:
            self._handle_event({
                "type": "error",
                "message": str(exc),
                "stage": "account_runtime",
            })
            raise
        finally:
            try:
                if self.facebook_action is not None:
                    close_action = getattr(self.facebook_action, "close", None)
                    if callable(close_action):
                        close_action()
            finally:
                if self.api is not None:
                    self.api.close()


def check_facebook_token(profile_id, account_key, action_factory=None):
    """Check the latest persisted token without TTC login or account mutation."""
    profile = get_profile_snapshot(profile_id)
    account = next(
        (
            item for item in profile.get("fb_accounts", [])
            if facebook_account_key(item) == account_key
        ),
        None,
    )
    if account is None:
        raise KeyError("Không tìm thấy Facebook account")
    token = str(account.get("token") or "").strip()
    if not token:
        raise ValueError("Facebook account chưa có token.")

    action = (action_factory or AUTO_Action)()
    try:
        check_live = getattr(action, "check_live", None)
        if not callable(check_live):
            raise TypeError("Facebook action không hỗ trợ kiểm tra LIVE token.")
        return _normalize_facebook_live_result(check_live(token))
    finally:
        close_action = getattr(action, "close", None)
        if callable(close_action):
            close_action()


def get_profile_balance(profile_id):
    """Return TTC account info for GUI/background callers."""
    api = None
    with profile_run_lock(profile_id):
        try:
            profile = get_profile_snapshot(profile_id)
            auth_token = str(profile.get("auth_token") or "").strip()
            if not auth_token:
                raise ValueError("Cần TTC token.")
            api = API_TTC(auth_token, worker_id=f"{profile_id}:balance")
            api.login()
            return dict(api.get_account_info())
        finally:
            if api is not None:
                api.close()


def get_profile_jobs_preview(profile_id, account_key=None):
    """Fetch current normalized jobs for GUI/background callers."""
    api = None
    action = None
    with profile_run_lock(profile_id):
        try:
            profile = get_profile_snapshot(profile_id)
            auth_token = str(profile.get("auth_token") or "").strip()
            if not auth_token:
                raise ValueError("Cần TTC token.")
            accounts = profile.get("fb_accounts", [])
            if account_key:
                account = next(
                    (item for item in accounts
                     if facebook_account_key(item) == account_key),
                    None,
                )
            else:
                selected = selected_accounts(profile)
                account = selected[0] if selected else next(
                    (item for item in accounts
                     if str(item.get("token") or "").strip()),
                    None,
                )
            if account is None:
                raise ValueError("Cần ít nhất một Facebook token.")
            api = API_TTC(auth_token, worker_id=f"{profile_id}:preview")
            api.login()
            action = AUTO_Action()
            uid, message = action.get_info(str(account.get("token") or "").strip())
            if not uid:
                raise RuntimeError(f"Không thể lấy UID Facebook: {message}")
            api.set_facebook_uid(uid)
            jobs = []
            for group in ("like_facebook", "like_poster"):
                result = api.fetch_jobs_for_legacy_type(group)
                jobs.extend(result["jobs"])
            return {"uid": uid, "jobs": jobs}
        finally:
            try:
                if action is not None:
                    action.close()
            finally:
                if api is not None:
                    api.close()


def run_multi_tokens(auth_token, fb_accounts, delay_between_jobs=None, max_jobs_per_acc=30,
                     continuous=False, job_types=None, normal_delay_min=None,
                     normal_delay_max=None, failed_delay_min=None, failed_delay_max=None,
                     like_batch_limit=None, comment_batch_limit=None,
                     like_batch_min=None, like_batch_max=None,
                     comment_batch_min=None, comment_batch_max=None,
                     like_delay_min=None, like_delay_max=None,
                     comment_delay_min=None, comment_delay_max=None,
                     account_delay_min=None, account_delay_max=None,
                     profile_id=None, skip_hidden_comments=True):
    enabled_types = normalize_job_types(job_types)
    like_batch_range, comment_batch_range = resolve_burst_ranges(
        like_batch_limit, comment_batch_limit,
        like_batch_min, like_batch_max,
        comment_batch_min, comment_batch_max,
    )
    like_delay_range, comment_delay_range, failed_delay_range, account_delay_range = resolve_run_delay_ranges(
        normal_delay_min, normal_delay_max, failed_delay_min, failed_delay_max,
        delay_between_jobs, like_delay_min, like_delay_max,
        comment_delay_min, comment_delay_max,
        account_delay_min, account_delay_max,
    )
    ttc_fetch_state = TTCFetchState()
    blocked_accounts_this_session = set()
    print_job_table_header()
    while True:
        for idx, acc in enumerate(fb_accounts):
            fb_token = str(acc.get('token') or '').strip()
            if not fb_token:
                print_warning(f"Tài khoản {idx+1} chưa có token, bỏ qua.")
                continue
            account_key = facebook_account_key(acc)
            if account_key in blocked_accounts_this_session:
                continue
            identity = str(acc.get('email') or '').strip() or f"Account {idx+1}"
            api = None
            facebook_action = None
            try:
                worker_id = f"{profile_id or 'profile'}:{idx+1}"
                api = API_TTC(auth_token, worker_id=worker_id)
                api.use_fetch_state(ttc_fetch_state)
                api.login()
                facebook_action = AUTO_Action()
                run_ttc_jobs(
                    api,
                    fb_token,
                    max_jobs=max_jobs_per_acc,
                    job_types=enabled_types,
                    like_batch_min=like_batch_range[0],
                    like_batch_max=like_batch_range[1],
                    comment_batch_min=comment_batch_range[0],
                    comment_batch_max=comment_batch_range[1],
                    like_delay_range=like_delay_range,
                    comment_delay_range=comment_delay_range,
                    failed_delay_range=failed_delay_range,
                    skip_hidden_comments=skip_hidden_comments,
                    action=facebook_action,
                )
            except FacebookInteractionBlocked as exc:
                blocked_accounts_this_session.add(account_key)
                print_warning(str(exc))
            except TTCAuthError as exc:
                if not print_ttc_exception("Phiên TTC không còn hợp lệ.", exc):
                    print_error(_mask_sensitive_text(str(exc)))
                return
            except Exception as exc:
                if not print_ttc_exception(f"Lỗi TTC với {identity}.", exc):
                    print_error(_mask_sensitive_text(f"Lỗi với {identity}: {exc}"))
            finally:
                try:
                    if facebook_action is not None:
                        close_action = getattr(facebook_action, "close", None)
                        if callable(close_action):
                            close_action()
                finally:
                    if api:
                        api.close()
            if idx < len(fb_accounts) - 1:
                account_delay = random.randint(*account_delay_range)
                _cooperative_countdown(account_delay, "Chuyển account")

        if blocked_accounts_this_session and not any(
            str(acc.get('token') or '').strip()
            and facebook_account_key(acc) not in blocked_accounts_this_session
            for acc in fb_accounts
        ):
            break
        if not continuous:
            break


def run_active_profile(profile):
    auth_token = str(profile.get('auth_token') or '').strip()
    accounts = selected_accounts(profile)
    if not auth_token:
        print_error("TTC profile chưa có token.")
        return
    if not accounts:
        print_warning("Chưa có Facebook account nào được bật để chạy.")
        return
    config = normalize_run_config(profile.get('run_config'))
    _runtime_log(None, "info", run_config_summary(config))
    try:
        with profile_run_lock(profile.get('profile_id')):
            _runtime_log(
                None, "info",
                f"Đã khóa profile TTC {profile['name']} cho phiên chạy này.",
            )
            run_multi_tokens(
                auth_token,
                accounts,
                max_jobs_per_acc=config['max_jobs_per_acc'],
                continuous=config['continuous'],
                job_types=set(config['job_types']),
                like_batch_min=config['like_batch_min'],
                like_batch_max=config['like_batch_max'],
                comment_batch_min=config['comment_batch_min'],
                comment_batch_max=config['comment_batch_max'],
                like_delay_min=config['like_delay_min'],
                like_delay_max=config['like_delay_max'],
                comment_delay_min=config['comment_delay_min'],
                comment_delay_max=config['comment_delay_max'],
                failed_delay_min=config['failed_delay_min'],
                failed_delay_max=config['failed_delay_max'],
                account_delay_min=config['account_delay_min'],
                account_delay_max=config['account_delay_max'],
                profile_id=profile['profile_id'],
                skip_hidden_comments=config['skip_hidden_comments'],
            )
    except RuntimeError as e:
        print_error(str(e))


def _first_selected_fb_token(profile):
    accounts = selected_accounts(profile)
    if accounts:
        return str(accounts[0].get('token') or '').strip()
    for account in profile.get('fb_accounts', []):
        token = str(account.get('token') or '').strip()
        if token:
            return token
    return ""


def check_balance(profile):
    auth_token = str(profile.get('auth_token') or '').strip()
    if not auth_token:
        print_error("Cần TTC token.")
        return
    api = None
    try:
        with profile_run_lock(profile.get('profile_id')):
            worker_id = f"{profile.get('profile_id') or 'profile'}:balance"
            api = API_TTC(auth_token, worker_id=worker_id)
            api.login()
            account_info = api.get_account_info()
            balance = account_info['balance']
            print_info(f"Số dư TTC {profile['name']}: {Fore.YELLOW}{balance}{Fore.CYAN} xu")
    except RuntimeError as e:
        print_error(str(e))
    except Exception as exc:
        if not print_ttc_exception("Lỗi TTC khi kiểm tra số dư.", exc):
            print_error(f"Lỗi: {exc}")
    finally:
        if api:
            api.close()


def show_jobs_for_profile(profile):
    auth_token = str(profile.get('auth_token') or '').strip()
    fb_token = _first_selected_fb_token(profile)
    if not auth_token or not fb_token:
        print_error("Cần TTC token và ít nhất một Facebook token.")
        return
    api = None
    action = None
    try:
        with profile_run_lock(profile.get('profile_id')):
            worker_id = f"{profile.get('profile_id') or 'profile'}:preview"
            api = API_TTC(auth_token, worker_id=worker_id)
            api.login()
            action = AUTO_Action()
            uid, message = action.get_info(fb_token)
            if not uid:
                print_error(f"Không thể lấy UID Facebook: {message}")
                return
            api.set_facebook_uid(uid)

            jobs = []
            for group in ("like_facebook", "like_poster"):
                result = api.fetch_jobs_for_legacy_type(group)
                jobs.extend(result['jobs'])
            if jobs:
                print_info(f"Tổng số jobs: {len(jobs)}")
                for idx, job in enumerate(jobs, start=1):
                    source = job.get('ttc_source') or job.get('type', 'unknown')
                    action_group = job.get('action_group') or job.get('type', 'unknown')
                    job_id = job.get('job_id') or job.get('buff_id', 'N/A')
                    details = [
                        f"{idx}. {action_group}",
                        f"TTC: {source}",
                        f"Job ID: {job_id}",
                        f"Object ID: {job.get('object_id', 'N/A')}",
                    ]
                    if job.get('reaction_type'):
                        details.append(f"Reaction: {job['reaction_type']}")
                    if job.get('comment_text'):
                        details.append(f"Comment: {job['comment_text']}")
                    print(" | ".join(details))
            else:
                print_info("Không có job nào.")
    except RuntimeError as e:
        print_error(str(e))
    except Exception as exc:
        if not print_ttc_exception("Lỗi TTC khi lấy danh sách job.", exc):
            print_error(f"Lỗi: {exc}")
    finally:
        try:
            if action is not None:
                action.close()
        finally:
            if api:
                api.close()


def print_menu(profile):
    print("\n" + "=" * 42)
    print(f"{Fore.MAGENTA}             TTC AUTO TOOL{Style.RESET_ALL}")
    print("=" * 42)
    print()
    if profile:
        accounts = profile.get('fb_accounts', [])
        selected = selected_account_keys(profile)
        enabled_count = sum(1 for acc in accounts if facebook_account_key(acc) in selected and str(acc.get("token", "")).strip())
        print(f"Profile   : {profile['name']}")
        print(f"Facebook  : {enabled_count}/{len(accounts)}\n")
    else:
        print("Profile   : CHƯA CHỌN")
        print("Facebook  : 0/0\n")

    print("1. Chạy job")
    print("2. Quản lý Facebook")
    print("3. Quản lý TTC")
    print("0. Thoát")


def ttc_manager():
    while True:
        data = load_database()
        profile = get_profile(data)
        print(f"\n{Fore.YELLOW}======== QUẢN LÝ TTC ========{Style.RESET_ALL}\n")
        if profile:
            accounts = profile.get('fb_accounts', [])
            print(f"Profile   : {profile['name']}")
            print(f"TTC token : {'Có' if str(profile.get('auth_token') or '').strip() else 'Chưa có'}")
            print(f"Facebook  : {len(selected_accounts(profile))} bật / {len(accounts)} tổng\n")
        else:
            print("Profile   : CHƯA CHỌN")
            print("TTC token : Chưa có")
            print("Facebook  : 0 bật / 0 tổng\n")
        print("1. Chọn profile")
        print("2. Tạo profile")
        print("3. Cập nhật TTC token")
        print("4. Kiểm tra số dư")
        print("5. Xem jobs")
        print("6. Quản lý profile")
        print("0. Quay lại")
        choice = input("Chọn (0-6): ").strip()
        if choice == '1':
            select_profile_menu(data)
        elif choice == '2':
            create_profile(data)
        elif choice == '3':
            view_ttc_token(data)
        elif choice == '4':
            if profile:
                check_balance(profile)
            else:
                print_error("Chưa có TTC profile.")
        elif choice == '5':
            if profile:
                show_jobs_for_profile(profile)
            else:
                print_error("Chưa có TTC profile.")
        elif choice == '6':
            profile_manager(data)
        elif choice == '0':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


def main():
    global SESSION_PROFILE_ID
    os.system("cls" if os.name == "nt" else "clear")
    try:
        data = load_database()
    except Exception as exc:
        print_error(f"Không thể đọc {DATA_FILE}: {exc}")
        return

    if not data['profiles']:
        print_warning("Chưa có TTC profile. Tạo profile đầu tiên.")
        create_profile(data)
        data = load_database()

    if SESSION_PROFILE_ID is None:
        SESSION_PROFILE_ID = data.get('active_profile_id')

    while True:
        data = load_database()
        profile = get_profile(data)
        print_menu(profile)
        choice = input("Chọn chức năng (0-3): ").strip()
        if choice == '1':
            if profile:
                config = configure_run(profile)
                if config is not None:
                    run_profile = dict(profile)
                    run_profile['run_config'] = config
                    run_active_profile(run_profile)
            else:
                print_error("Chưa có TTC profile.")
        elif choice == '2':
            if profile:
                account_manager(profile['profile_id'])
            else:
                print_error("Chưa có TTC profile.")
        elif choice == '3':
            ttc_manager()
        elif choice == '0':
            print_success("Thoát chương trình.")
            break
        else:
            print_error("Lựa chọn không hợp lệ.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if _runtime_debug_enabled():
            raise
        print("\nĐã dừng.")
