# Codex Session Manager

Một công cụ mã nguồn mở được thiết kế theo kiến trúc **Zero-Dependency** và **Cross-Platform (Windows, macOS, Linux)** để quản lý, sao chép, chuyển đổi nhà cung cấp (Switch Provider kèm Auto-Sync) và đồng bộ hoá 2 chiều các phiên làm việc (sessions / threads) giữa **OpenAI Codex** và **DeepSeek** (hoặc các nhà cung cấp mô hình khác).

---

## 🌟 Tính Năng Nổi Bật

1. **Tự Động Đồng Bộ Khi Chuyển Đổi (Switch Provider with Auto-Sync):**
   - Khi bạn click chuyển sang DeepSeek hoặc OpenAI, script **tự động đồng bộ các turn mới nhất trước**, sau đó mới đổi trạng thái hiển thị trên thanh bên. Bạn không cần phải nhớ chạy sync riêng!
2. **Cơ Sở Dữ Liệu Ánh Xạ Chuyên Biệt (`session_manager.sqlite`):**
   - Lưu trữ rõ ràng từng cặp session `(openai_thread_id <---> deepseek_thread_id)` kèm thời gian đồng bộ lần cuối (`last_synced_at`).
   - Không còn phụ thuộc vào việc tìm kiếm tên mập mờ.
3. **Tuyệt Đối An Toàn Khi Unarchive (Chống Khôi Phục Nhầm):**
   - Khi chuyển đổi qua lại giữa OpenAI và DeepSeek, script **CHỈ thao tác đúng các Thread ID đã đăng ký trong mapping DB**.
   - Bất kỳ session nào bạn đã tự tay archive trước đây (ví dụ các bản test cũ, project đã huỷ) đều được **giữ nguyên 100%, không bao giờ bị unarchive nhầm**.
4. **Đồng Bộ Hai Chiều An Toàn (Append-Only Sync):**
   - So khớp danh sách turn giữa bản gốc và bản clone `(ds)`.
   - Chỉ nối (append) các turn hội thoại mới mà không ghi đè hay xoá bất kỳ dữ liệu cũ nào.
   - Tự động tạo snapshot backup trước mỗi lần đồng bộ.
5. **Zero External Dependencies:**
   - Hoàn toàn viết bằng thư viện chuẩn của Python 3 (`sqlite3`, `json`, `uuid`, `time`, `os`, `shutil`, `sys`, `re`).
   - Không cần chạy `pip install` bất cứ package nào.

---

## 📂 Cấu Trúc Dự Án (Modular Architecture)

```
codex-session-manager/
├── .git/                      # Git repository
├── .gitignore
├── README.md                  # Tài liệu hướng dẫn chi tiết
├── codex_migrator.py          # Script thực thi chính (Entrypoint)
├── codex_manager/             # Python package lõi
│   ├── __init__.py
│   ├── __main__.py            # Hỗ trợ chạy: python -m codex_manager
│   ├── cli.py                 # Xử lý tham số dòng lệnh & dispatching
│   ├── config.py              # Nhận diện đường dẫn & trừu tượng hoá OS
│   ├── db.py                  # Thao tác SQLite state_5 & thread_history_1
│   ├── mapping.py             # Quản lý mapping DB (session_manager.sqlite)
│   ├── migration.py           # Logic clone session & rollback an toàn
│   ├── sync.py                # Thuật toán đồng bộ hai chiều append-only
│   ├── switch.py              # Quản lý chuyển đổi provider & auto-sync
│   └── rollout.py             # Parser & Serializer cho định dạng JSONL Rollout wire
├── scripts/                   # Các script tiện ích
│   ├── switch-deepseek.bat    # 1-click Auto-sync + Chuyển sang DeepSeek
│   ├── switch-openai.bat      # 1-click Auto-sync + Chuyển sang OpenAI
│   └── sync-sessions.bat      # 1-click đồng bộ thủ công
└── tests/
    └── test_smoke.py          # Kiểm thử tự động (Smoke tests)
```

---

## 🚀 Hướng Dẫn Sử Dụng

### 1. Xem danh sách các cặp session đã ánh xạ
```bash
python codex_migrator.py pairs
```

### 2. Chuyển đổi chế độ (Tự động Sync trước khi đổi thanh bên)
- **Tự động sync + Ẩn OpenAI gốc, chuyển sang DeepSeek:**
  ```bash
  python codex_migrator.py switch deepseek
  ```
- **Tự động sync + Ẩn DeepSeek, chuyển sang OpenAI:**
  ```bash
  python codex_migrator.py switch openai
  ```
- **Hiển thị toàn bộ cả 2 bên:**
  ```bash
  python codex_migrator.py switch all
  ```
*(Muốn chuyển nhanh mà không sync: thêm cờ `--no-sync`)*

### 3. Đồng bộ hai chiều thủ công
```bash
# Đồng bộ tất cả các cặp
python codex_migrator.py sync all

# Đồng bộ một cặp cụ thể
python codex_migrator.py sync Grok
```

### 4. Quản lý cặp session thủ công (nếu cần)
```bash
# Thêm cặp mới vào DB
python codex_migrator.py pair add <openai_id_hoặc_tên> <deepseek_id_hoặc_tên> [tên_dự_án]

# Xoá một cặp khỏi DB
python codex_migrator.py pair remove <tên_hoặc_id>
```

### 5. Tạo bản sao mới sang DeepSeek
```bash
python codex_migrator.py migrate <THREAD_ID_HOẶC_TÊN> deepseek
```
*(Bản clone mới sẽ tự động được ghi nhận vào mapping database)*
