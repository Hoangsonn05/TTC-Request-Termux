# TTC AUTO TOOL

Công cụ dòng lệnh tự động xử lý job Tương Tác Chéo (TTC) với Facebook trên Termux và môi trường Python tương thích.

## Chạy chương trình

Yêu cầu Python 3 và các dependency: `requests`, `colorama`, `pyotp`.

```bash
python api_ttc.py
```

`api_ttc.py` là production entrypoint và cũng là toàn bộ production source của project. Các file `test_ttc_*.py` là regression tests, không phải runtime module.

## Job TTC được hỗ trợ

- `LIKE_POST_VIP_CHEO`
- `LIKE_POST_VIP_RE`
- `REACTION_VIP_CHEO`
- `REACTION_VIP_RE`
- `COMMENT`

Không hỗ trợ `FOLLOW` và `LIKE_PAGE`. Runtime không fetch hoặc claim hai loại job này.

Scheduler giữ hai bridge name nội bộ `like_facebook` và `like_poster` để tương thích với cấu hình/runtime hiện có. Chúng không phải API của nền tảng khác.

## TTC UID contract

UID Facebook được cấu hình qua request:

```text
POST /cauhinh/datnick.php
iddat[]=<facebook_uid>
loai=fb
```

Chỉ response body chính xác bằng `"1"` sau khi loại transport whitespace mới được xem là thành công. HTTP lỗi hoặc body khác đều fail closed.

## Credential và dữ liệu local

- TTC `acctoken` được lưu dưới compatibility key `auth_token`.
- TTC token và Facebook token hiển thị theo policy CLI hiện tại.
- Password và mã 2FA được nhập ẩn.
- Facebook login dùng cùng app authorization được bundle trong implementation tham chiếu Bumx; không cần cấu hình OAuth runtime riêng cho TTC.
- `.env` và `.env.*` chỉ dành cho cấu hình local, không commit.

Các tên storage cũ có thể tiếp tục chứa chữ `bumx` để profile/account/config hiện có vẫn load được:

- biến môi trường `BUMX_TERMUX_DATA_FILE`
- `bumx_termux_data.json`
- `.bumx_runtime/`

Đây chỉ là compatibility contract của persistence, không đại diện cho Bumx platform API. Không xóa hoặc đổi tên các mục này nếu chưa có một task migration dữ liệu riêng.

## Runtime contracts

- Raw TTC log in nguyên văn `server_raw_text`; classification nội bộ được ghi riêng.
- Comment chạy theo chuỗi decode → Facebook → verify → settle 5 giây → TTC claim.
- Comment `PENDING_APPROVAL` không claim TTC, dừng Comment trong lượt account hiện tại và fallback sang Like; outer cycle sau thử Comment lại.
- Cooldown tạm thời áp dụng cho đúng cặp `(uid, TTC job_type)`; permanent skip chỉ áp dụng đúng TTC job type trong run hiện tại.
- Facebook timeout là action failure sạch: không ép `None` thành số và không submit TTC.

## Kiểm thử offline

Regression suite dùng fake/mocked responses và không cần production network:

```bash
python -m unittest -v
python -m compileall -q .
git diff --check
```

Không giảm coverage hoặc sửa expected result chỉ để làm suite xanh.

## Bảo mật

Không commit TTC credential, Facebook account credential, file runtime hoặc `.env`. Không in/log app authorization đã bundle trong implementation Facebook login.
