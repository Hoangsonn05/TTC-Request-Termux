import requests
import time
import hmac
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

from ttc_adapter import API_TTC

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

init(autoreset=True)

total_coin = 0
SUPPORTED_JOB_TYPES = frozenset({'like_facebook', 'like_poster'})


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

    filtered_jobs = [job for job in jobs if job.get('type') in enabled_types]
    if enabled_types != SUPPORTED_JOB_TYPES:
        return filtered_jobs[:max_jobs]

    queues = {
        'like_facebook': [job for job in filtered_jobs if job.get('type') == 'like_facebook'],
        'like_poster': [job for job in filtered_jobs if job.get('type') == 'like_poster'],
    }
    ranges = {'like_facebook': like_range, 'like_poster': comment_range}
    positions = {'like_facebook': 0, 'like_poster': 0}
    current_type = 'like_facebook' if queues['like_facebook'] else 'like_poster'
    scheduled = []

    while len(scheduled) < max_jobs:
        other_type = 'like_poster' if current_type == 'like_facebook' else 'like_facebook'
        current_position = positions[current_type]
        current_queue = queues[current_type]
        if current_position >= len(current_queue):
            if positions[other_type] >= len(queues[other_type]):
                break
            current_type = other_type
            continue

        burst_target = random.randint(*ranges[current_type])
        batch_size = min(
            burst_target,
            len(current_queue) - current_position,
            max_jobs - len(scheduled),
        )
        scheduled.extend(current_queue[current_position:current_position + batch_size])
        positions[current_type] += batch_size
        if positions[other_type] < len(queues[other_type]):
            current_type = other_type
    return scheduled


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
def print_success(msg):
    print(f"{Fore.GREEN}✅ {msg}{Style.RESET_ALL}")

def print_error(msg):
    print(f"{Fore.RED}❌ {msg}{Style.RESET_ALL}")

def print_info(msg):
    print(f"{Fore.CYAN}ℹ️ {msg}{Style.RESET_ALL}")

def print_warning(msg):
    print(f"{Fore.YELLOW}⚠️ {msg}{Style.RESET_ALL}")

def print_debug(msg):
    print(f"{Fore.MAGENTA}🐞 {msg}{Style.RESET_ALL}")

# ---------- Hàm tạo HMAC ----------
def build_hmac_header(method: str, path: str) -> str:
    timestamp_ms = int(time.time() * 1000)
    payload = f"{timestamp_ms}.{method}.{path}"
    key = b"bembembemzz"
    digest = hmac.new(key, payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"{timestamp_ms}:{digest}"

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
            raise ProfileInUseError(f"Profile Bumx {profile_id} đang được sử dụng ở phiên khác.")
    try:
        yield
    finally:
        if fd is not None:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

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


def _new_profile(name, auth_token="", fb_accounts=None):
    return {
        "profile_id": uuid.uuid4().hex,
        "name": str(name).strip() or "Bumx",
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
        raise ValueError("Danh sách Bumx profile không hợp lệ")
    normalized_profiles = []
    ids = set()
    for raw in data["profiles"]:
        if not isinstance(raw, dict):
            raise ValueError("Bumx profile không hợp lệ")
        profile_id = str(raw.get("profile_id", "")).strip()
        if not profile_id or profile_id in ids:
            raise ValueError("profile_id không hợp lệ hoặc bị trùng")
        ids.add(profile_id)
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("Tên Bumx profile không được trống")
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
            profile = _new_profile("Bumx mặc định", auth, accounts)
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
        raise KeyError("Không tìm thấy Bumx profile")
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
        print_error(f"Token Bumx đã refresh trong phiên hiện tại nhưng không thể lưu dữ liệu do xung đột/IO: {exc}")
        return False


def rename_profile(profile_id, new_name):
    new_name = str(new_name or "").strip()
    if not new_name:
        raise ValueError("Tên không được trống.")

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy Bumx profile")
        profile["name"] = new_name
        return profile["name"]

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def update_profile_auth_token_manual(profile_id, token):
    token = str(token or "").strip()

    def _mutator(data):
        profile = get_profile(data, profile_id)
        if profile is None:
            raise KeyError("Không tìm thấy Bumx profile")
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
        raise KeyError("Không tìm thấy Bumx profile")

    with profile_run_lock(profile_id):
        return mutate_database(_mutator)


def select_profile_as_default(profile_id):
    def _mutator(data):
        if get_profile(data, profile_id) is None:
            raise KeyError("Không tìm thấy Bumx profile")
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
    """Compatibility helper: trả config của Bumx profile đang chọn."""
    data = load_database()
    profile = get_profile(data)
    if profile is None:
        return {"auth_token": None, "fb_accounts": []}
    return {"auth_token": profile.get("auth_token") or None, "fb_accounts": list(profile["fb_accounts"])}


def save_config(auth_token, fb_accounts):
    """Compatibility helper: chỉ lưu vào Bumx profile đang chọn."""
    def _mutator(data):
        profile = get_profile(data)
        if profile is None:
            profile = _new_profile("Bumx mặc định", auth_token, fb_accounts)
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
        }, data=payload)

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
        }, data=payload)

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
            }, data=payload)
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
class AUTO_Action:
    def __init__(self):
        self.url = 'https://b-graph.facebook.com/graphql'
        self._requests = requests.Session()
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

    def get_info(self, token):
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
        res = self._requests.post(url=self.url, headers={
            "Authorization": f"OAuth {token}",
            "X-FB-Friendly-Name": "FbStoriesLightBucketsQuery",
        }, data=payload)
        try:
            res_json = res.json()
        except:
            return None, "Lỗi parse JSON (Có thể token die or Acc bị cp 282)"
        if "errors" in res_json:
            for err in res_json["errors"]:
                if "access_token" in err.get("message", "").lower():
                    return None, "Token đã die (hết hạn hoặc không hợp lệ)"
                return None, err.get("message", "Lỗi GraphQL")
        user_id = res_json.get("data", {}).get("me", {}).get("id")
        if user_id:
            return user_id, "OK"
        return None, "Không tìm thấy user_id trong response (có thể token die)"

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
        res = self._requests.post(url=self.url, headers={
            "Authorization": f"OAuth {token}",
            "X-FB-Friendly-Name": "ViewerReactionsMutation",
        }, data=payload)
        try:
            result = res.json()
        except:
            print("❌ Lỗi parse JSON")
            print(res.text[:500])
            return False
        if "errors" in result:
            for err in result["errors"]:
                print(f"❌ {err.get('summary')}: {err.get('description')}")
            return False
        feedback = result.get("data", {}).get("feedback_react", {}).get("feedback", {})
        if feedback.get("viewer_feedback_reaction_key") is not None:
            print("✅ Reaction thành công!")
            return True
        else:
            print("⚠️ Reaction không thành công hoặc phản hồi thiếu dữ liệu.")
            return False

    def auto_cmt(self, post_id, text, token, actor_id):
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
        res = self._requests.post(url=self.url, headers={
            "Authorization": f"OAuth {token}",
            "X-FB-Friendly-Name": "CommentCreateMutation",
        }, data=payload)
        try:
            result = res.json()
        except:
            print("❌ Lỗi parse JSON")
            print(res.text[:500])
            return False
        if "errors" in result:
            for err in result["errors"]:
                print(f"❌ {err.get('summary')}: {err.get('description')}")
            return False
        else:
            comment_data = result.get("data", {}).get("comment_create", {}).get("comment")
            if comment_data and comment_data.get("id"):
                print("✅ Comment thành công! ID:", comment_data["id"])
                return True
            else:
                print("⚠️ Phản hồi thành công nhưng thiếu dữ liệu comment.")
                return False

# ---------- Hàm lấy post ID ----------
def get_facebook_post_id(url):
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
    session = requests.Session()
    session.headers.update(headers)
    try:
        resp = session.get(url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            print(f"Lỗi HTTP {resp.status_code}")
            return None
        final_url = resp.url
    except Exception as e:
        print(f"Lỗi kết nối: {e}")
        return None
    resp = session.get(final_url, timeout=15, allow_redirects=True)
    html = resp.text
    try:
        uid = html.split('"post_id":"')[1].split('",')[0]
        print(f"{Fore.GREEN}{Style.BRIGHT}✅ Post ID: {uid}{Style.RESET_ALL}")
        return uid
    except:
        return 0

# ---------- Hàm đếm delay ----------
def countdown(seconds, message="Chờ"):
    for i in range(seconds, 0, -1):
        print(f"\r{Fore.YELLOW}⏳ {message} {i} giây...{Style.RESET_ALL}", end="", flush=True)
        time.sleep(1)
    print(f"\r{Fore.GREEN}✅ Tiếp tục!{' ' * 20}{Style.RESET_ALL}")

# ---------- Class API_BUMX ----------
class API_BUMX:
    def __init__(self, auth, fb_token, normal_delay_min=None, normal_delay_max=None,
                 failed_delay_min=None, failed_delay_max=None, delay_between_jobs=None,
                 like_delay_min=None, like_delay_max=None, comment_delay_min=None,
                 comment_delay_max=None, profile_id=None):
        self.refresh_token = auth
        self.fb_token = fb_token
        self.profile_id = profile_id
        (
            self.like_delay_range,
            self.comment_delay_range,
            self.failed_delay_range,
            _,
        ) = resolve_run_delay_ranges(
            normal_delay_min, normal_delay_max, failed_delay_min, failed_delay_max,
            delay_between_jobs, like_delay_min, like_delay_max,
            comment_delay_min, comment_delay_max, None, None,
        )
        self.session = requests.Session()
        self.base_headers = {
            'accept-encoding': 'gzip',
            'host': 'api-v2.bumx.vn',
            'lang': 'vi',
            'origin': 'app',
            'platform': 'android',
            'user-agent': 'Dart/3.11 (dart:io)',
            'version': '42',
            'visitor': 'PI',
        }
        self.session.headers.update(self.base_headers)
        self.api_paths = {
            'token': '/api/authorization/refresh-token',
            'wallet': '/api/business/wallet',
            'get-new-mission': '/api/buff/get-new-mission',
            'mission': '/api/buff/mission',
            'report-buff': '/api/buff/report-buff',
            'load-mission': '/api/buff/load-mission',
            'submit-mission': '/api/buff/submit-mission',
        }
        self.base_url = 'https://api-v2.bumx.vn'
        self._refresh_token()

    def _refresh_token(self):
        try:
            resp = self.session.post(
                self.base_url + self.api_paths['token'],
                json={'refreshToken': self.refresh_token}
            )
            if resp.status_code == 200:
                data = resp.json().get('data', {})
                if isinstance(data, str):
                    data = json.loads(data)
                new_token = data.get('accessToken')
                if new_token:
                    self.access_token = new_token
                    self.session.headers.update({"authorization": self.access_token})
                    if self.profile_id:
                        update_profile_auth_token(self.profile_id, new_token)
                    print_success(f"Token đã được refresh: {preview_token(new_token)}")
                    return True
            else:
                print_error(f"Refresh token thất bại: {resp.status_code}")
                return False
        except Exception as e:
            print_error(f"Lỗi khi refresh token: {e}")
            return False

    def _request(self, method, path, json_data=None, extra_headers=None, retry=True):
        hmac_header = build_hmac_header(method, path)
        headers = {'hmac': hmac_header}
        if json_data is not None:
            headers['content-type'] = 'application/json'
        if extra_headers:
            headers.update(extra_headers)

        url = self.base_url + path
        if method.upper() == 'GET':
            resp = self.session.get(url, headers=headers, params=json_data)
        else:
            resp = self.session.post(url, headers=headers, json=json_data)

        if resp.status_code == 401 and retry:
            print_warning("AccessToken hết hạn, đang refresh...")
            if self._refresh_token():
                return self._request(method, path, json_data, extra_headers, retry=False)
        return resp

    def get_info_account(self):
        path = self.api_paths['wallet']
        resp = self._request('GET', path)
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            return data.get('balance', 0)
        return 0

    def get_mission_jobs(self):
        path = self.api_paths['mission']
        resp = self._request('GET', path, json_data={'is_from_mobile': True})
        if resp.status_code != 200:
            return []
        try:
            payload = resp.json()
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        jobs = payload.get('data')
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            return []
        return jobs

    def get_new_mission_jobs(self, mission_type):
        path = self.api_paths['get-new-mission']
        resp = self._request('POST', path, json_data={'type': mission_type})
        if resp.status_code != 200:
            return []
        try:
            payload = resp.json()
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        jobs = payload.get('data')
        if not isinstance(jobs, list):
            return []
        return jobs

    def load_mission(self, buff_id):
        path = self.api_paths['load-mission']
        resp = self._request('POST', path, json_data={'buff_id': buff_id})
        return resp.json()

    def REPORT(self, buff_id):
        path = self.api_paths['report-buff']
        resp = self._request('POST', path, json_data={'buff_id': buff_id})
        return resp.json()

    def submit(self, buff_id, type_jobs, cmt_id=None, cmt="tt nha"):
        path = self.api_paths['submit-mission']
        payload = {
            "buff_id": buff_id,
            "comment": cmt,
            "comment_id": cmt_id,
            "code_submit": None,
            "attachments": [],
            "link_share": "",
            "code": "",
            "is_from_mobile": True,
            "type": type_jobs,
            "sub_id": None,
            "data": None
        }
        resp = self._request('POST', path, json_data=payload)
        return resp.json()

    def run_jobs(self, max_jobs=30, job_types=None, like_batch_limit=None,
                 comment_batch_limit=None, like_batch_min=None, like_batch_max=None,
                 comment_batch_min=None, comment_batch_max=None):
        global total_coin
        enabled_types = normalize_job_types(job_types)
        do_job = AUTO_Action()
        uid, msg = do_job.get_info(self.fb_token)
        if uid:
            print("UID:" + str(uid))
        else:
            print('ERROR:' + msg)
            return 0

        print_info("Đang refresh token...")
        self._refresh_token()

        print_info("Đang kiểm tra job mới từ get-new-mission...")
        while True:
            like_data = self.get_new_mission_jobs('like_facebook') if 'like_facebook' in enabled_types else []
            comment_data = self.get_new_mission_jobs('like_poster') if 'like_poster' in enabled_types else []
            like_count = len(like_data)
            comment_count = len(comment_data)
            total_new_jobs = like_count + comment_count
            print_info(f"get-new-mission: {like_count} job like, {comment_count} job comment")
            if total_new_jobs > 0:
                break
            print_info("Không có job mới.")
            countdown(300, "Không có job, chờ trước lần lấy job tiếp theo")

        print_info("Đang lấy danh sách job từ mission...")
        mission_jobs = self.get_mission_jobs()
        print_info(f"mission: {len(mission_jobs)} job")
        if not mission_jobs:
            print_info("Không có job nào để làm.")
            return 0

        jobs = schedule_jobs_by_burst(
            mission_jobs,
            enabled_types,
            max_jobs,
            like_batch_limit=like_batch_limit,
            comment_batch_limit=comment_batch_limit,
            like_batch_min=like_batch_min,
            like_batch_max=like_batch_max,
            comment_batch_min=comment_batch_min,
            comment_batch_max=comment_batch_max,
        )
        if not jobs:
            print_info("Không có job thuộc loại đã chọn.")
            return 0

        print_info(f"Scheduler đã chọn {len(jobs)} job, hard cap {max_jobs}.")
        total_jobs = len(jobs)
        total_done = 0

        for idx, job in enumerate(jobs, start=1):
            buff_id = job.get('buff_id')
            type_ = job.get('type')
            job_succeeded = False
            if not buff_id:
                print_warning("Job thiếu buff_id, bỏ qua.")
            else:
                job_type_display = "LIKE" if type_ == 'like_facebook' else "CMT"
                coin_reward = 20 if type_ == 'like_facebook' else 50
                print_info(f"[{idx}/{total_jobs}] Đang xử lý job {type_} (buff_id: {buff_id})...")

                if type_ == 'like_facebook':
                    try:
                        job_detail = self.load_mission(buff_id)
                        object_id = job_detail.get('object_id')
                        if not object_id:
                            print_warning("Không có object_id, bỏ qua.")
                            self.REPORT(buff_id)
                        else:
                            icon = job_detail.get('icon', '')
                            if 'icon_like.png' in icon:
                                reaction = 'LIKE'
                            elif 'icon_love.png' in icon:
                                reaction = 'LOVE'
                            elif 'icon_thuongthuong.png' in icon:
                                reaction = 'CARE'
                            elif 'icon_haha.png' in icon:
                                reaction = 'HAHA'
                            elif 'icon_wow.png' in icon:
                                reaction = 'WOW'
                            elif 'icon_sad.png' in icon:
                                reaction = 'SAD'
                            elif 'icon_angry.png' in icon:
                                reaction = 'ANGRY'
                            else:
                                reaction = 'LIKE'

                            print_info(f"   Thực hiện {reaction} trên bài viết {object_id}")
                            post_id = get_facebook_post_id(f'https://www.facebook.com/{object_id}')
                            if int(post_id) > 0:
                                status = do_job.auto_reaction(post_id, reaction, self.fb_token, uid)
                                time.sleep(3)
                                if status:
                                    result = self.submit(buff_id, 'like_facebook')
                                    if result.get('success'):
                                        job_succeeded = True
                                        total_done += 1
                                        total_coin += coin_reward
                                        msg = result.get('message', '')
                                        print_success(f"[{idx}/{total_jobs}] {job_type_display} | +{coin_reward} | Tổng: {total_coin} | {msg}")
                                    else:
                                        print_warning(f"[{idx}/{total_jobs}] {job_type_display} | Submit thất bại, báo cáo buff {buff_id}")
                                        self.REPORT(buff_id)
                                else:
                                    print_warning(f"[{idx}/{total_jobs}] {job_type_display} | Like thất bại, báo cáo buff {buff_id}")
                                    self.REPORT(buff_id)
                            else:
                                print_warning(f"[{idx}/{total_jobs}] {job_type_display} | Lấy id thất bại {buff_id}")
                                self.REPORT(buff_id)
                    except Exception as exc:
                        print_error(f"[{idx}/{total_jobs}] Lỗi like_facebook: {exc}")
                        self.REPORT(buff_id)

                elif type_ == 'like_poster':
                    try:
                        job_detail = self.load_mission(buff_id)
                        object_id = job_detail.get('object_id')
                        cmt = job_detail.get('data', '')
                        if not object_id:
                            print_warning("Không có object_id, bỏ qua.")
                            self.REPORT(buff_id)
                        else:
                            try:
                                if "\\u" in cmt:
                                    cmt = cmt.encode('utf-8').decode('unicode_escape')
                            except Exception:
                                pass
                            cmt_id = job_detail.get('comment_id')
                            print_info(f"   Comment vào bài viết {object_id}")
                            post_id = get_facebook_post_id(f'https://www.facebook.com/{object_id}')
                            if int(post_id) > 0:
                                status = do_job.auto_cmt(post_id, cmt, self.fb_token, uid)
                                time.sleep(3)
                                if status:
                                    result = self.submit(buff_id, 'like_poster', cmt_id, cmt)
                                    if result.get('success'):
                                        job_succeeded = True
                                        total_done += 1
                                        total_coin += coin_reward
                                        msg = result.get('message', '')
                                        print_success(f"[{idx}/{total_jobs}] {job_type_display} | +{coin_reward} | Tổng: {total_coin} | {msg}")
                                    else:
                                        print_warning(f"[{idx}/{total_jobs}] {job_type_display} | Submit thất bại, báo cáo buff {buff_id}")
                                        self.REPORT(buff_id)
                                else:
                                    print_warning(f"[{idx}/{total_jobs}] {job_type_display} | Comment thất bại, báo cáo buff {buff_id}")
                                    self.REPORT(buff_id)
                            else:
                                print_warning(f"[{idx}/{total_jobs}] {job_type_display} | Lấy id thất bại {buff_id}")
                                self.REPORT(buff_id)
                    except Exception as exc:
                        print_error(f"[{idx}/{total_jobs}] Lỗi like_poster: {exc}")
                        self.REPORT(buff_id)

            if idx < total_jobs:
                if job_succeeded:
                    delay_range = self.like_delay_range if type_ == 'like_facebook' else self.comment_delay_range
                else:
                    delay_range = self.failed_delay_range
                delay = random.randint(*delay_range)
                countdown(delay, f"Chờ trước job {idx+1}")

        print_info(f"Hoàn thành {total_done}/{total_jobs} job. Tổng coin: {total_coin}")
        return total_done

    def show_jobs(self):
        jobs = self.get_mission_jobs()
        if not jobs:
            self.get_new_mission_jobs('like_facebook')
            self.get_new_mission_jobs('like_poster')
            jobs = self.get_mission_jobs()
        if not jobs:
            return []
        job_list = []
        for job in jobs:
            buff_id = job.get('buff_id')
            if buff_id:
                detail = self.load_mission(buff_id)
                object_id = detail.get('object_id', 'N/A') if detail else 'N/A'
                job_list.append({
                    'buff_id': buff_id,
                    'object_id': object_id,
                    'type': job.get('type', 'unknown')
                })
        return job_list

    def close(self):
        pass

# ---------- Quản lý Bumx profile / Facebook accounts ----------
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
            raise ValueError("Facebook account/token đã tồn tại trong Bumx profile này.")
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

    req_fb = req_fb or Requests_facebook()
    token = req_fb.login_and_get_token(email, password, secret)
    if not token:
        print_error(f"Đăng nhập thất bại cho {email}.")
        return False

    try:
        persist_status = _persist_login_account(profile_id, email, password, secret, token)
        if persist_status == "profile_missing":
            print_warning("Không tìm thấy profile")
            return False
        if persist_status == "duplicate":
            print_warning("Facebook account đã tồn tại trong Bumx profile này.")
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
            print_error("Bumx profile không còn tồn tại. Dừng batch login.")
            results.append({"email": email, "status": "profile_missing"})
            stop_batch = True
            break

        print_info(f"[{index}/{total}] Đang login UID/email {email}...")
        try:
            req_fb = req_fb_factory() if req_fb_factory is not None else Requests_facebook()
            token = req_fb.login_and_get_token(
                email, account["password"], account["secret"]
            )
        except Exception:
            token = None

        if not token:
            print_error(f"[{index}/{total}] Login thất bại cho {email}.")
            results.append({"email": email, "status": "login_failed"})
            try:
                if get_profile(load_database(), profile_id) is None:
                    print_error("Bumx profile không còn tồn tại. Dừng batch login.")
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
                    f"[{index}/{total}] Không thể thêm {email} vì Bumx profile đang được sử dụng."
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
                print_error("Bumx profile không còn tồn tại. Dừng batch login.")
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
    print(f"\n{Fore.YELLOW}Facebook của Bumx: {profile['name']}{Style.RESET_ALL}")
    if not accounts:
        print_info("Chưa có Facebook account.")
        return
    for index, account in enumerate(accounts, start=1):
        key = facebook_account_key(account)
        run_mark = "✅" if key in selected else "❌"
        identity = str(account.get('email', '')).strip() or "Manual token"
        token_view = preview_token(account.get('token', ''))
        has_2fa = "Có" if str(account.get('secret', '')).strip() else "Không"
        print(f" {index:>2}. {run_mark} {identity} | token {token_view} | 2FA: {has_2fa}")


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
    token = Requests_facebook().login_and_get_token(email, password, secret)
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


def account_manager(profile_id):
    while True:
        try:
            profile = get_profile(load_database(), profile_id)
        except Exception as exc:
            print_error(f"Không thể đọc dữ liệu mới nhất. {exc}")
            return
        if profile is None:
            print_error("Bumx profile không còn tồn tại trong dữ liệu mới nhất.")
            return
        print(f"\n{Fore.YELLOW}=== FACEBOOK — {profile['name']} ==={Style.RESET_ALL}")
        print("1. Xem danh sách")
        print("2. Xem chi tiết account")
        print("3. Thêm token thủ công")
        print("4. Login Facebook hàng loạt & thêm")
        print("5. Sửa account")
        print("6. Login lại & cập nhật token")
        print("7. Xóa account")
        print("8. Chọn Facebook account được chạy")
        print("9. Quay lại")
        choice = input("Chọn (1-9): ").strip()
        if choice == '1':
            view_accounts(profile)
        elif choice == '2':
            show_facebook_account_details(profile)
        elif choice == '3':
            token = input("Facebook token: ")
            if add_manual_account(profile_id, token):
                print_success("Đã thêm token.")
        elif choice == '4':
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
        elif choice == '5':
            edit_account(profile)
        elif choice == '6':
            relogin_account(profile)
        elif choice == '7':
            delete_account(profile)
        elif choice == '8':
            toggle_account_selection(profile)
        elif choice == '9':
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
        f"continuous={'ON' if config['continuous'] else 'OFF'}"
    )


def configure_run(profile):
    config = normalize_run_config(profile.get('run_config'))
    while True:
        print(f"\n{Fore.YELLOW}=== CẤU HÌNH CHẠY — {profile['name']} ==={Style.RESET_ALL}")
        print(run_config_summary(config))
        print("1. Loại job")
        print("2. Chạy liên tục")
        print("3. Max job / Facebook account")
        print("4. Burst Like min-max")
        print("5. Burst Comment min-max")
        print("6. Delay Like min-max")
        print("7. Delay Comment min-max")
        print("8. Delay khi job lỗi min-max")
        print("9. Delay chuyển Facebook account min-max")
        print("10. Reset mặc định")
        print("11. Quay lại")
        choice = input("Chọn (1-11): ").strip()
        candidate = dict(config)
        changed_fields = ()
        if choice == '1':
            print("1. Like | 2. Comment | 3. Cả hai")
            job_choice = input("Chọn: ").strip()
            if job_choice == '1':
                candidate['job_types'] = ['like_facebook']
            elif job_choice == '2':
                candidate['job_types'] = ['like_poster']
            elif job_choice == '3':
                candidate['job_types'] = ['like_facebook', 'like_poster']
            else:
                print_error("Lựa chọn không hợp lệ.")
                continue
            changed_fields = ('job_types',)
        elif choice == '2':
            candidate['continuous'] = not config['continuous']
            changed_fields = ('continuous',)
        elif choice == '3':
            candidate['max_jobs_per_acc'] = prompt_int(
                f"Max jobs [{config['max_jobs_per_acc']}]: ", config['max_jobs_per_acc'], 1
            )
            changed_fields = ('max_jobs_per_acc',)
        elif choice == '4':
            candidate['like_batch_min'], candidate['like_batch_max'] = prompt_range(
                "Like burst", config['like_batch_min'], config['like_batch_max'], 1
            )
            changed_fields = ('like_batch_min', 'like_batch_max')
        elif choice == '5':
            candidate['comment_batch_min'], candidate['comment_batch_max'] = prompt_range(
                "Comment burst", config['comment_batch_min'], config['comment_batch_max'], 1
            )
            changed_fields = ('comment_batch_min', 'comment_batch_max')
        elif choice == '6':
            candidate['like_delay_min'], candidate['like_delay_max'] = prompt_range(
                "Like delay", config['like_delay_min'], config['like_delay_max'], 0
            )
            changed_fields = ('like_delay_min', 'like_delay_max')
        elif choice == '7':
            candidate['comment_delay_min'], candidate['comment_delay_max'] = prompt_range(
                "Comment delay", config['comment_delay_min'], config['comment_delay_max'], 0
            )
            changed_fields = ('comment_delay_min', 'comment_delay_max')
        elif choice == '8':
            candidate['failed_delay_min'], candidate['failed_delay_max'] = prompt_range(
                "Failed delay", config['failed_delay_min'], config['failed_delay_max'], 0
            )
            changed_fields = ('failed_delay_min', 'failed_delay_max')
        elif choice == '9':
            candidate['account_delay_min'], candidate['account_delay_max'] = prompt_range(
                "Account delay", config['account_delay_min'], config['account_delay_max'], 0
            )
            changed_fields = ('account_delay_min', 'account_delay_max')
        elif choice == '10':
            candidate = normalize_run_config()
            changed_fields = tuple(
                field for field in DEFAULT_RUN_CONFIG if field != 'selected_fb_account_keys'
            )
        elif choice == '11':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")
            continue
        candidate = normalize_run_config(candidate)

        profile_id = profile.get("profile_id")
        def _mutator(data):
            prof = get_profile(data, profile_id)
            if not prof:
                raise ValueError("Không tìm thấy profile")
            latest_config = normalize_run_config(prof.get('run_config'))
            for field in changed_fields:
                latest_config[field] = candidate[field]
            prof['run_config'] = latest_config
            return dict(latest_config)

        try:
            with profile_run_lock(profile_id):
                config = mutate_database(_mutator)
                print_success("Đã lưu cấu hình chạy.")
        except RuntimeError as e:
            print_error(str(e))
        except ValueError as e:
            print_error(str(e))


def list_profiles(data):
    profiles = data.get('profiles', [])
    if not profiles:
        print_info("Chưa có Bumx profile.")
        return
    active = SESSION_PROFILE_ID if SESSION_PROFILE_ID is not None else data.get('active_profile_id')
    for index, profile in enumerate(profiles, start=1):
        mark = "▶️ " if profile['profile_id'] == active else "   "
        fb_count = len(profile.get('fb_accounts', []))
        print(f"{index}. {mark}{profile['name']} | FB: {fb_count}")


def _profile_index(data, prompt="Chọn profile để chạy: "):
    if not data.get('profiles'):
        print_warning("Chưa có Bumx profile.")
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
    print("Nhập Bumx token:")
    token = input().strip()

    print(f"\n{Fore.YELLOW}=== XÁC NHẬN PROFILE ==={Style.RESET_ALL}")
    print(f"Tên:\n{name}")
    print(f"\nBumx token:\n{token}")
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


def view_bumx_token(data):
    index = _profile_index(data, "Chọn profile muốn xem/cập nhật token: ")
    if index is None:
        return
    profile = data['profiles'][index]
    profile_id = profile['profile_id']
    profile_name = profile['name']
    while True:
        print(f"\n{Fore.YELLOW}=== BUMX TOKEN — {profile['name']} ==={Style.RESET_ALL}")
        print("Token hiện tại:\n")
        print(profile.get('auth_token') or '(trống)')
        print("\n1. Giữ nguyên")
        print("2. Nhập token mới")
        print("3. Quay lại")
        choice = input("Chọn (1-3): ").strip()
        if choice == '1':
            return
        elif choice == '2':
            print("\nNhập Bumx token mới:")
            token = input().strip()
            print("\nToken vừa nhập:\n")
            print(token)
            if input("\nLưu token này? (y/n): ").strip().lower() in ['y', 'yes']:
                try:
                    update_profile_auth_token_manual(profile_id, token)
                    print_success("Đã cập nhật Bumx token.")
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


def profile_manager(data):
    global SESSION_PROFILE_ID
    while True:
        try:
            latest = load_database()
            data.clear()
            data.update(latest)
        except Exception as exc:
            print_error(f"Không thể đọc dữ liệu mới nhất. {exc}")
            return
        print(f"\n{Fore.YELLOW}=== QUẢN LÝ BUMX PROFILE ==={Style.RESET_ALL}\n")
        active = SESSION_PROFILE_ID or data.get('active_profile_id')
        active_name = "CHƯA CHỌN"
        for p in data.get('profiles', []):
            if p['profile_id'] == active:
                active_name = p['name']
                break
        print("Profile của tab này:")
        print(f"▶️ {active_name}\n")
        print("1. Chọn profile để chạy")
        print("2. Tạo profile mới")
        print("3. Đổi tên profile")
        print("4. Xem / cập nhật Bumx token")
        print("5. Sao chép Facebook từ profile khác")
        print("6. Xóa profile")
        print("7. Quay lại")
        choice = input("Chọn (1-7): ").strip()
        if choice == '1':
            print(f"\n{Fore.YELLOW}=== CHỌN PROFILE ĐỂ CHẠY ==={Style.RESET_ALL}")
            index = _profile_index(data)
            if index is not None:
                selected_id = data['profiles'][index]['profile_id']
                selected_name = data['profiles'][index]['name']
                try:
                    select_profile_as_default(selected_id)
                    SESSION_PROFILE_ID = selected_id
                    print_success(f"Đã chọn profile để chạy: {selected_name}")
                except Exception as exc:
                    print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")
        elif choice == '2':
            create_profile(data)
        elif choice == '3':
            print(f"\n{Fore.YELLOW}=== ĐỔI TÊN PROFILE ==={Style.RESET_ALL}")
            index = _profile_index(data, "Chọn profile muốn đổi tên: ")
            if index is not None:
                profile_id = data['profiles'][index]['profile_id']
                old_name = data['profiles'][index]['name']
                name = input("Tên mới: ").strip()
                if name:
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
        elif choice == '4':
            view_bumx_token(data)
        elif choice == '5':
            copy_facebook_accounts_from_profile(SESSION_PROFILE_ID)
        elif choice == '6':
            print(f"\n{Fore.YELLOW}=== XÓA PROFILE ==={Style.RESET_ALL}")
            index = _profile_index(data, "Chọn profile muốn xóa: ")
            if index is not None:
                profile = data['profiles'][index]
                fb_count = len(profile.get('fb_accounts', []))
                print(f"\n=== XÁC NHẬN XÓA PROFILE ===")
                print(f"Profile:\n{profile['name']}")
                print(f"\nFacebook:\n{fb_count} account")
                print(f"\nBumx token:\n{profile.get('auth_token') or '(trống)'}")
                if input(f"\nXóa profile này? (y/n): ").strip().lower() in ['y', 'yes']:
                    deleted_id = profile['profile_id']
                    deleted_name = profile['name']
                    try:
                        _, new_active_id = delete_profile(deleted_id)
                        if SESSION_PROFILE_ID == deleted_id:
                            SESSION_PROFILE_ID = new_active_id
                        print_success("Đã xóa Bumx profile.")
                    except ProfileInUseError:
                        print_error(
                            f'Không thể xóa. Profile "{deleted_name}" '
                            'đang được sử dụng ở phiên khác.'
                        )
                    except Exception as exc:
                        print_error(f"Không thể lưu thay đổi. Dữ liệu cũ vẫn được giữ nguyên. {exc}")
        elif choice == '7':
            return
        else:
            print_error("Lựa chọn không hợp lệ.")


# ---------- Chạy đa Facebook account ----------
def run_multi_tokens(auth_token, fb_accounts, delay_between_jobs=None, max_jobs_per_acc=30,
                     continuous=False, job_types=None, normal_delay_min=None,
                     normal_delay_max=None, failed_delay_min=None, failed_delay_max=None,
                     like_batch_limit=None, comment_batch_limit=None,
                     like_batch_min=None, like_batch_max=None,
                     comment_batch_min=None, comment_batch_max=None,
                     like_delay_min=None, like_delay_max=None,
                     comment_delay_min=None, comment_delay_max=None,
                     account_delay_min=None, account_delay_max=None,
                     profile_id=None):
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
    cycle = 1
    while True:
        print_info(f"=== CHU KỲ {cycle} ===")
        total_all = 0
        for idx, acc in enumerate(fb_accounts):
            fb_token = str(acc.get('token') or '').strip()
            if not fb_token:
                print_warning(f"Tài khoản {idx+1} chưa có token, bỏ qua.")
                continue
            identity = str(acc.get('email') or '').strip() or f"Account {idx+1}"
            print_info(f"\n--- {identity} ({idx+1}/{len(fb_accounts)}) ---")
            api = None
            try:
                api = API_BUMX(
                    auth_token,
                    fb_token,
                    like_delay_min=like_delay_range[0],
                    like_delay_max=like_delay_range[1],
                    comment_delay_min=comment_delay_range[0],
                    comment_delay_max=comment_delay_range[1],
                    failed_delay_min=failed_delay_range[0],
                    failed_delay_max=failed_delay_range[1],
                    profile_id=profile_id,
                )
                done = api.run_jobs(
                    max_jobs=max_jobs_per_acc,
                    job_types=enabled_types,
                    like_batch_min=like_batch_range[0],
                    like_batch_max=like_batch_range[1],
                    comment_batch_min=comment_batch_range[0],
                    comment_batch_max=comment_batch_range[1],
                )
                total_all += done
                print_info(f"{identity} đã làm {done} job.")
            except Exception as exc:
                print_error(f"Lỗi với {identity}: {exc}")
            finally:
                if api:
                    api.close()
            if idx < len(fb_accounts) - 1:
                account_delay = random.randint(*account_delay_range)
                countdown(account_delay, "Chờ trước Facebook account tiếp theo")

        print_info(f"Hoàn thành chu kỳ {cycle}, tổng job: {total_all}")
        if not continuous:
            break
        countdown(20, "Chờ trước chu kỳ tiếp theo")
        cycle += 1


def run_active_profile(profile):
    auth_token = str(profile.get('auth_token') or '').strip()
    accounts = selected_accounts(profile)
    if not auth_token:
        print_error("Bumx profile chưa có token.")
        return
    if not accounts:
        print_warning("⚠️ Chưa có Facebook account nào được bật để chạy.")
        return
    config = normalize_run_config(profile.get('run_config'))
    print_info(run_config_summary(config))
    try:
        with profile_run_lock(profile.get('profile_id')):
            print_info(f"🔒 Đã khóa profile Bumx {profile['name']} cho phiên chạy này.")
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
    fb_token = _first_selected_fb_token(profile)
    if not auth_token or not fb_token:
        print_error("Cần Bumx token và ít nhất một Facebook token.")
        return
    api = None
    try:
        with profile_run_lock(profile.get('profile_id')):
            api = API_BUMX(auth_token, fb_token, profile_id=profile['profile_id'])
            balance = api.get_info_account()
            print_info(f"Số dư ví {profile['name']}: {Fore.YELLOW}{balance}{Fore.CYAN} xu")
    except RuntimeError as e:
        print_error(str(e))
    except Exception as exc:
        print_error(f"Lỗi: {exc}")
    finally:
        if api:
            api.close()


def show_jobs_for_profile(profile):
    auth_token = str(profile.get('auth_token') or '').strip()
    fb_token = _first_selected_fb_token(profile)
    if not auth_token or not fb_token:
        print_error("Cần Bumx token và ít nhất một Facebook token.")
        return
    api = None
    try:
        with profile_run_lock(profile.get('profile_id')):
            api = API_BUMX(auth_token, fb_token, profile_id=profile['profile_id'])
            jobs = api.show_jobs()
            if jobs:
                print_info(f"Tổng số jobs: {len(jobs)}")
                for idx, job in enumerate(jobs, start=1):
                    print(f"{idx}. {job['type']} - Buff ID: {job['buff_id']} - Object ID: {job['object_id']}")
            else:
                print_info("Không có job nào.")
    except RuntimeError as e:
        print_error(str(e))
    except Exception as exc:
        print_error(f"Lỗi: {exc}")
    finally:
        if api:
            api.close()


def print_menu(profile):
    print("\n" + "=" * 42)
    print(f"{Fore.MAGENTA}BUMX AUTO TOOL{Style.RESET_ALL}")
    print("=" * 42)
    print()
    if profile:
        print("Profile của tab này:")
        print(f"▶️ {profile['name']}\n")

        accounts = profile.get('fb_accounts', [])
        selected = selected_account_keys(profile)
        enabled_count = sum(1 for acc in accounts if facebook_account_key(acc) in selected and str(acc.get("token", "")).strip())
        print("Facebook đang bật:")
        print(f"{enabled_count}/{len(accounts)}\n")
    else:
        print("Profile của tab này:")
        print("▶️ CHƯA CHỌN\n")
        print("Facebook đang bật:")
        print("0/0\n")

    print("1. Chạy tự động")
    print("2. Kiểm tra số dư")
    print("3. Xem jobs")
    print("4. Quản lý Facebook")
    print("5. Cấu hình delay / burst")
    print("6. Quản lý Bumx profile")
    print("7. Thoát")


def main():
    global SESSION_PROFILE_ID
    os.system("cls" if os.name == "nt" else "clear")
    try:
        data = load_database()
    except Exception as exc:
        print_error(f"Không thể đọc {DATA_FILE}: {exc}")
        return

    if not data['profiles']:
        print_warning("Chưa có Bumx profile. Tạo profile đầu tiên.")
        create_profile(data)
        data = load_database()

    if SESSION_PROFILE_ID is None:
        SESSION_PROFILE_ID = data.get('active_profile_id')

    while True:
        data = load_database()
        profile = get_profile(data)
        print_menu(profile)
        choice = input("Chọn chức năng (1-7): ").strip()
        if choice == '1':
            if profile:
                run_active_profile(profile)
            else:
                print_error("Chưa có Bumx profile.")
        elif choice == '2':
            if profile:
                check_balance(profile)
            else:
                print_error("Chưa có Bumx profile.")
        elif choice == '3':
            if profile:
                show_jobs_for_profile(profile)
            else:
                print_error("Chưa có Bumx profile.")
        elif choice == '4':
            if profile:
                account_manager(profile['profile_id'])
            else:
                print_error("Chưa có Bumx profile.")
        elif choice == '5':
            if profile:
                configure_run(profile)
            else:
                print_error("Chưa có Bumx profile.")
        elif choice == '6':
            profile_manager(data)
        elif choice == '7':
            print_success("Thoát chương trình.")
            break
        else:
            print_error("Lựa chọn không hợp lệ.")


if __name__ == "__main__":
    main()
