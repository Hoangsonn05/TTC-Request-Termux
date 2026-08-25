# TTC API Full

`TTC_API_FULL.py` là module Python độc lập chứa toàn bộ phần giao tiếp API Tương Tác Chéo (TTC) được trích xuất từ dự án `TTC_FB_Gui_Gemlogin`.

Module chỉ phụ trách tầng API và chuẩn hóa dữ liệu. Nó không phụ thuộc giao diện GUI, GemLogin, Playwright, ADB hoặc logic thực hiện thao tác Facebook. Vì vậy có thể sao chép trực tiếp sang một tool Python khác để tích hợp.

## 1. Chức năng chính

Module hiện hỗ trợ:

- Đăng nhập TTC bằng `access_token`.
- Đọc tên tài khoản TTC và số dư ban đầu.
- Cấu hình UID Facebook dùng để nhận job.
- Lấy danh sách job từ 7 nhóm endpoint TTC.
- Chuẩn hóa dữ liệu job về một schema thống nhất.
- Chuẩn hóa loại cảm xúc Facebook.
- Tách nội dung comment từ dữ liệu TTC.
- Chuẩn hóa Facebook object ID từ ID thô hoặc URL.
- Submit job để nhận xu sau khi tool đã thực hiện thao tác thành công.
- Đọc số xu nhận được và số dư mới nếu response có cung cấp.
- Phân loại lỗi đăng nhập, hết job, rate limit, lỗi HTTP, response sai định dạng và lỗi claim.
- Tạo một HTTP session riêng cho từng `TTCClient`, phù hợp với mô hình nhiều worker.
- Che token trong log của `TTCClient`; giao diện profile tương tác vẫn hiển thị token TTC theo chính sách người dùng.

> Module không tự động like, thả cảm xúc, comment, follow hoặc like page. Tool tích hợp phải tự thực hiện hành động trên Facebook, xác minh kết quả rồi mới gọi `submit_job()`.

## 2. Yêu cầu môi trường

- Python 3.10 trở lên.
- Thư viện bắt buộc: `requests`.
- Thư viện tùy chọn: `cloudscraper`.

Cài đặt:

```bash
pip install requests
```

Cài thêm `cloudscraper` nếu cần session có khả năng xử lý một số lớp bảo vệ HTTP tốt hơn:

```bash
pip install cloudscraper
```

Nếu `cloudscraper` không được cài đặt, module tự động sử dụng `requests.Session`.

## 3. Cấu trúc file API

File `TTC_API_FULL.py` được chia thành các khối chính sau.

### 3.1. Khai báo URL và endpoint

Các hằng số chính:

```python
TTC_BASE_URL
TTC_AUTH_URL
TTC_SET_UID_URL
TTC_ENDPOINTS
```

`TTC_ENDPOINTS` lưu tập trung cặp endpoint lấy job và nhận tiền theo cấu trúc:

```python
{
    "job_type": (
        "get_job_url",
        "claim_reward_url",
    )
}
```

Việc tập trung URL tại một vị trí giúp dễ kiểm tra và thay đổi endpoint mà không cần tìm URL rải rác trong mã nguồn.

### 3.2. Danh sách loại job

Module cung cấp hai cách truy cập loại job:

- Chuỗi trong `ALL_JOB_TYPES`.
- Enum `TTCJobType`, được khuyến nghị khi tích hợp.

```python
from TTC_API_FULL import TTCJobType

TTCJobType.FOLLOW
TTCJobType.COMMENT
TTCJobType.LIKE_PAGE
```

### 3.3. Phân nhóm hành động

Các job được quy về bốn nhóm hành động nội bộ:

| Action group | Ý nghĩa |
|---|---|
| `reaction` | Like hoặc thả cảm xúc bài viết |
| `comment` | Bình luận bài viết |
| `follow` | Theo dõi tài khoản |
| `page_like` | Like trang Facebook |

Hai cấu trúc liên quan:

```python
ACTION_GROUP_MAP
ACTION_GROUPS_BLOCKED_BY
```

`ACTION_GROUPS_BLOCKED_BY` có thể dùng trong tool để tạm khóa toàn bộ nhóm job khi một loại thao tác gặp lỗi hoặc bị giới hạn.

### 3.4. Hàm chuẩn hóa dữ liệu

Các hàm public quan trọng:

```python
normalize_reaction_type(raw)
normalize_job_type(job_type)
get_action_group(job_type)
get_endpoint_urls(job_type)
normalize_object_id(object_id)
normalize_job(job_type, raw_data)
normalize_jobs_list(job_type, raw_list)
parse_ttc_comment_text(nd)
```

Mục đích của lớp chuẩn hóa là để phần còn lại của tool không phải xử lý riêng từng định dạng response TTC.

### 3.5. Các lớp exception

```python
TTCError
TTCAuthError
TTCNoJobAvailable
TTCRateLimitError
TTCResponseError
```

Tool tích hợp nên bắt riêng từng exception để có chiến lược xử lý phù hợp thay vì bắt chung toàn bộ lỗi.

### 3.6. Kết quả submit

`TTCClaimResult` là dataclass nội bộ dùng để chuẩn hóa kết quả claim. Hàm `submit_job()` trả về dictionary thông qua `TTCClaimResult.to_dict()`.

### 3.7. HTTP client chính

Lớp chính:

```python
TTCClient
```

Các method cấp thấp:

```python
authenticate(token)
configure_facebook_uid(uid)
fetch_jobs(job_type)
claim_reward(job)
get_account_info()
close()
```

Các alias thuận tiện khi tích hợp:

```python
login(token)
set_uid(uid)
get_jobs(job_type)
submit_job(job)
```

## 4. Các loại job được hỗ trợ

| Enum | Giá trị job type | Hành động |
|---|---|---|
| `TTCJobType.LIKE_POST_VIP_CHEO` | `ttc_like_post_vip_cheo` | Like bài viết VIP chéo |
| `TTCJobType.LIKE_POST_VIP_RE` | `ttc_like_post_vip_re` | Like bài viết VIP re |
| `TTCJobType.REACTION_VIP_CHEO` | `ttc_reaction_vip_cheo` | Thả cảm xúc VIP chéo |
| `TTCJobType.REACTION_VIP_RE` | `ttc_reaction_vip_re` | Thả cảm xúc VIP re |
| `TTCJobType.COMMENT` | `ttc_comment` | Bình luận |
| `TTCJobType.FOLLOW` | `ttc_follow` | Follow tài khoản |
| `TTCJobType.LIKE_PAGE` | `ttc_like_page` | Like page |

## 5. Danh sách endpoint

### 5.1. Đăng nhập

```text
POST https://tuongtaccheo.com/logintoken.php
```

Payload:

```python
{
    "access_token": "TTC_ACCESS_TOKEN"
}
```

### 5.2. Cấu hình UID Facebook

```text
POST https://tuongtaccheo.com/cauhinh/datnick.php
```

Dữ liệu gửi theo mã nguồn gốc:

```python
{
    "iddat[]": "FACEBOOK_UID",
    "loai": "fb"
}
```

Request dùng form data. TTC chỉ được xem là xác nhận thành công khi
`response.text.strip() == "1"`; không có success body nào khác được suy diễn.

### 5.3. Endpoint job

| Job type | Get job | Submit/nhận tiền |
|---|---|---|
| Like post VIP chéo | `/kiemtien/likepostvipcheo/getpost.php` | `/kiemtien/likepostvipcheo/nhantien.php` |
| Like post VIP re | `/kiemtien/likepostvipre/getpost.php` | `/kiemtien/likepostvipre/nhantien.php` |
| Cảm xúc VIP chéo | `/kiemtien/camxucvipcheo/getpost.php` | `/kiemtien/camxucvipcheo/nhantien.php` |
| Cảm xúc VIP re | `/kiemtien/camxucvipre/getpost.php` | `/kiemtien/camxucvipre/nhantien.php` |
| Comment | `/kiemtien/cmtcheo/getpost.php` | `/kiemtien/cmtcheo/nhantien.php` |
| Follow | `/kiemtien/subcheo/getpost.php` | `/kiemtien/subcheo/nhantien.php` |
| Like page | `/kiemtien/likepagecheo/getpost.php` | `/kiemtien/likepagecheo/nhantien.php` |

Tất cả đường dẫn trên sử dụng base URL:

```text
https://tuongtaccheo.com
```

## 6. Payload submit theo loại job

### 6.1. Like post, comment, follow và like page

```python
{
    "id": "IDPOST_FROM_TTC"
}
```

### 6.2. Job thả cảm xúc

```python
{
    "id": "IDPOST_FROM_TTC",
    "loaicx": "LOVE"
}
```

`loaicx` có thể là:

```text
LIKE
LOVE
CARE
HAHA
WOW
SAD
ANGRY
```

Hai loại `LIKE_POST` chỉ submit `id`. Hai loại `REACTION_VIP` submit cả `id` và `loaicx`.

## 7. Quy trình tích hợp chuẩn

Luồng xử lý được khuyến nghị:

```text
1. Tạo TTCClient.
2. Đăng nhập bằng token TTC.
3. Đặt UID Facebook.
4. Lấy job theo loại cần chạy.
5. Tool tự thực hiện hành động trên Facebook.
6. Xác minh hành động đã thành công.
7. Gọi submit_job(job) để nhận xu.
8. Xử lý kết quả submit hoặc lỗi.
9. Đóng session khi worker kết thúc.
```

Không nên submit trước khi thao tác Facebook thực sự hoàn tất.

## 8. Ví dụ sử dụng cơ bản

```python
from TTC_API_FULL import (
    TTCClient,
    TTCJobType,
    TTCNoJobAvailable,
    TTCAuthError,
    TTCRateLimitError,
    TTCResponseError,
)

TOKEN = "YOUR_TTC_ACCESS_TOKEN"
FACEBOOK_UID = "YOUR_FACEBOOK_UID"

client = TTCClient(worker_id="1")

try:
    account = client.login(TOKEN)
    print("Tài khoản:", account)

    client.set_uid(FACEBOOK_UID)

    jobs = client.get_jobs(TTCJobType.FOLLOW)

    for job in jobs:
        print("Job nhận được:", job)

        # Tool tích hợp tự thực hiện hành động Facebook tại đây.
        facebook_action_success = True

        if not facebook_action_success:
            continue

        result = client.submit_job(job)
        print("Kết quả submit:", result)

except TTCNoJobAvailable as exc:
    print("Không có job:", exc)
    print("Thời gian chờ:", exc.countdown)

except TTCAuthError as exc:
    print("Lỗi xác thực:", exc)

except TTCRateLimitError as exc:
    print("Bị giới hạn request:", exc)

except TTCResponseError as exc:
    print("Lỗi API TTC:", exc)

finally:
    client.close()
```

## 9. Lấy nhiều loại job

```python
from TTC_API_FULL import TTCClient, TTCJobType, TTCNoJobAvailable

job_types = [
    TTCJobType.LIKE_POST_VIP_CHEO,
    TTCJobType.REACTION_VIP_CHEO,
    TTCJobType.COMMENT,
    TTCJobType.FOLLOW,
    TTCJobType.LIKE_PAGE,
]

client = TTCClient(worker_id="worker-01")
client.login("YOUR_TTC_ACCESS_TOKEN")
client.set_uid("YOUR_FACEBOOK_UID")

try:
    for job_type in job_types:
        try:
            jobs = client.get_jobs(job_type)
            print(job_type.value, len(jobs))
        except TTCNoJobAvailable as exc:
            print(job_type.value, "không có job", exc.countdown)
finally:
    client.close()
```

## 10. Schema job sau khi chuẩn hóa

`get_jobs()` trả về danh sách dictionary có cấu trúc thống nhất:

```python
{
    "job_id": "ttc_follow:123456789",
    "buff_id": "ttc_follow:123456789",
    "type": "ttc_follow",
    "action_group": "follow",
    "object_id": "123456789",
    "link": "https://facebook.com/123456789",
    "reaction_type": "",
    "comment_text": "",
    "ttc_source": "ttc_follow",
    "idpost": "123456789",
    "raw_data": {...},
}
```

Ý nghĩa các trường:

| Trường | Ý nghĩa |
|---|---|
| `job_id` | Khóa duy nhất được tạo từ loại job, `idpost` và cảm xúc nếu có |
| `buff_id` | Alias tương thích với một số hàng đợi job cũ |
| `type` | Loại job TTC nội bộ |
| `action_group` | Nhóm thao tác: reaction, comment, follow hoặc page_like |
| `object_id` | Facebook object ID đã chuẩn hóa |
| `link` | Link Facebook TTC trả về |
| `reaction_type` | Loại cảm xúc đã chuẩn hóa; rỗng với job không phải reaction |
| `comment_text` | Nội dung comment đã tách và làm sạch |
| `ttc_source` | Nguồn endpoint TTC |
| `idpost` | ID gốc dùng khi gọi endpoint nhận tiền |
| `raw_data` | Response job gốc, chỉ nên dùng để debug |

Không nên để GUI hoặc business logic phụ thuộc trực tiếp vào `raw_data` vì TTC có thể thay đổi format response.

## 11. Kết quả submit

`submit_job(job)` trả về dictionary có các trường:

```python
{
    "success": True,
    "reason": "SUCCESS",
    "message": "...",
    "earned_coin": 600,
    "balance": 12000,
    "http_status": 200,
    "retryable": False,
    "endpoint_key": "ttc_follow",
    "evidence": "...",
    "response_kind": "dict",
    "response_keys": ("success", "message"),
    "raw_summary": "...",
}
```

Các `reason` chính có thể xuất hiện:

| Reason | Ý nghĩa | Có thể thử lại |
|---|---|---|
| `SUCCESS` | Claim thành công | Không cần |
| `NOT_OBSERVED_YET` | TTC chưa ghi nhận thao tác | Có |
| `ALREADY_CLAIMED` | Job đã nhận tiền trước đó | Không |
| `AUTH_EXPIRED` | Phiên đăng nhập hết hạn | Không, cần đăng nhập lại |
| `RATE_LIMITED` | TTC giới hạn tốc độ request | Có, cần delay |
| `INVALID_JOB` | Job không tồn tại, sai hoặc đã bị hủy | Không |
| `INVALID_RESPONSE` | Server trả HTML hoặc dữ liệu không hợp lệ | Thường không nên retry ngay |
| `UNKNOWN_RESPONSE` | Không nhận diện được response | Không nên tự kết luận thành công |
| `TTC_REJECTED` | TTC từ chối claim | Phụ thuộc thông báo server |

Chỉ coi submit thành công khi:

```python
result["success"] is True
```

Không nên suy luận thành công chỉ từ HTTP status `200`.

## 12. Xử lý không có job

Khi TTC chưa có job, `get_jobs()` phát sinh:

```python
TTCNoJobAvailable
```

Exception này có thêm thuộc tính:

```python
exc.countdown
```

Ví dụ:

```python
try:
    jobs = client.get_jobs(TTCJobType.COMMENT)
except TTCNoJobAvailable as exc:
    wait_seconds = exc.countdown or 10
```

Tool nên dùng countdown TTC trả về nếu có, không nên request liên tục khi hết job.

## 13. Tích hợp đa luồng

Mỗi worker nên có một `TTCClient` riêng:

```python
client = TTCClient(worker_id=device_id)
```

Không nên dùng chung một instance cho nhiều tài khoản hoặc nhiều worker vì session chứa cookie và trạng thái đăng nhập TTC.

Mô hình khuyến nghị:

```text
Worker 1 -> TTCClient 1 -> TTC token/session 1
Worker 2 -> TTCClient 2 -> TTC token/session 2
Worker 3 -> TTCClient 3 -> TTC token/session 3
```

Khi worker kết thúc, luôn gọi:

```python
client.close()
```

## 14. Logging

Mặc định module log ra console:

```text
[TTC Worker #1] [AUTH] ...
```

Có thể truyền callback log riêng:

```python
def app_log(message, level):
    print(level, message)

client = TTCClient(worker_id="1", log_fn=app_log)
```

Token TTC không được log đầy đủ bởi `TTCClient`; module chỉ ghi hash rút gọn.
Trong CLI quản lý profile, token TTC được nhập và hiển thị nguyên văn để người
dùng tự đối chiếu. Password và 2FA secret vẫn dùng input ẩn.

## 15. Lưu ý khi tích hợp

- Không chỉnh sửa trực tiếp `idpost` trước khi submit.
- Luôn submit chính object job được trả về bởi `get_jobs()`.
- Với reaction job, giữ nguyên `reaction_type` để payload `loaicx` chính xác.
- Với comment job, sử dụng `comment_text` thay vì tự đọc trường `nd` từ `raw_data`.
  Parser chỉ decode HTML entity, trích wrapper dấu nháy và strip khoảng trắng;
  nội dung dài không bị cắt ngầm.
- Không request get job hoặc submit quá dày; cần cấu hình delay và backoff.
- Khi gặp `TTCRateLimitError`, nên tăng delay trước lần thử tiếp theo.
- Khi gặp `TTCAuthError`, cần đăng nhập lại hoặc yêu cầu token mới.
- Không lưu token TTC trực tiếp trong mã nguồn hoặc đẩy token lên Git.
- Không chia sẻ cùng một client/session cho nhiều token.
- API TTC có thể thay đổi phía server; khi response mới xuất hiện, cần kiểm tra `evidence`, `response_kind`, `response_keys` và `raw_summary`.

## 16. API report hoặc skip

Trong mã nguồn được trích xuất không có endpoint TTC độc lập cho chức năng `report` hoặc `skip`.

Vì vậy module hiện chỉ bao gồm các API thực tế đã được tìm thấy và sử dụng trong tool:

- Login.
- Cấu hình UID.
- Get job.
- Submit/nhận tiền.

Việc bỏ qua một job hiện được thực hiện ở business logic của tool bằng cách không submit job đó và chuyển sang job khác. Không nên tự tạo endpoint report/skip khi chưa có request thực tế được xác minh.

## 17. Danh sách export public

Module khai báo `__all__` cho các thành phần được phép import:

```python
TTC_BASE_URL
TTC_AUTH_URL
TTC_SET_UID_URL
TTC_ENDPOINTS
ALL_JOB_TYPES
ACTION_GROUP_MAP
ACTION_GROUPS_BLOCKED_BY
VALID_REACTIONS
TTCJobType
TTCClient
TTCClaimResult
TTCError
TTCAuthError
TTCNoJobAvailable
TTCRateLimitError
TTCResponseError
normalize_reaction_type
normalize_job_type
get_action_group
get_endpoint_urls
normalize_object_id
normalize_job
normalize_jobs_list
parse_ttc_comment_text
```

Có thể import toàn bộ public API bằng:

```python
from TTC_API_FULL import *
```

Tuy nhiên, trong dự án thực tế nên import rõ thành phần cần dùng để mã nguồn dễ kiểm soát hơn.

## 18. Cấu trúc thư mục đề xuất

```text
your_tool/
├── api/
│   ├── __init__.py
│   └── TTC_API_FULL.py
├── services/
│   └── ttc_worker.py
├── config/
│   └── settings.py
├── README.md
└── requirements.txt
```

`requirements.txt` tối thiểu:

```text
requests>=2.31.0
```

Hoặc khi dùng cloudscraper:

```text
requests>=2.31.0
cloudscraper>=1.2.71
```

## 19. Ví dụ import khi đặt trong thư mục `api`

Tạo `api/__init__.py`:

```python
from .TTC_API_FULL import (
    TTCClient,
    TTCJobType,
    TTCError,
    TTCAuthError,
    TTCNoJobAvailable,
    TTCRateLimitError,
    TTCResponseError,
)

__all__ = [
    "TTCClient",
    "TTCJobType",
    "TTCError",
    "TTCAuthError",
    "TTCNoJobAvailable",
    "TTCRateLimitError",
    "TTCResponseError",
]
```

Sau đó sử dụng:

```python
from api import TTCClient, TTCJobType
```

## 20. Phạm vi trách nhiệm

`TTC_API_FULL.py` chịu trách nhiệm:

```text
TTC authentication
TTC session
TTC endpoint routing
Get job
Normalize job
Submit job
Parse response
Classify API errors
```

Tool bên ngoài chịu trách nhiệm:

```text
Quản lý tài khoản Facebook
Mở link hoặc object Facebook
Thực hiện like/reaction/comment/follow/page like
Xác minh thao tác thành công
Delay và retry policy
Hàng đợi job
Quản lý worker/thread/device
Lưu lịch sử và thống kê
Giao diện người dùng
```

Việc tách hai phạm vi này giúp file API có thể tái sử dụng trong nhiều tool mà không phụ thuộc công nghệ điều khiển Facebook cụ thể.
