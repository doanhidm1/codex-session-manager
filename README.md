# Codex Session Manager

Một công cụ mã nguồn mở được thiết kế theo kiến trúc **Zero-Dependency** và **Cross-Platform (Windows, macOS, Linux)** để quản lý, sao chép, tập trung (focus mode) và đồng bộ hoá 2 chiều các phiên làm việc (sessions / threads) giữa **OpenAI Codex** và **DeepSeek** (hoặc các nhà cung cấp mô hình khác).

---

## 🌟 Tính Năng Nổi Bật

1. **Zero External Dependencies:**
   - Hoàn toàn viết bằng thư viện chuẩn của Python 3 (`sqlite3`, `json`, `uuid`, `time`, `os`, `shutil`, `sys`, `re`).
   - Không cần chạy `pip install` bất cứ package nào. Bất kỳ máy nào có Python 3.8+ đều chạy được ngay lập tức.
2. **Hỗ Trợ Cross-Platform Toàn Diện:**
   - Tự động nhận diện thư mục `.codex` trên Windows (`%USERPROFILE%\.codex`), macOS/Linux (`~/.codex`), hoặc biến môi trường `$CODEX_HOME`.
   - Hỗ trợ tham số `--codex-home <đường_dẫn>` để trỏ tới thư mục dữ liệu tuỳ chọn.
3. **Focus Mode (Ẩn Session Gốc trên PC Tránh Chat Nhầm):**
   - Ẩn/Hiện session bằng cách cập nhật trạng thái `archived` trong SQLite mà không làm mất dữ liệu.
   - Khi chuyển sang chế độ DeepSeek, các session gốc OpenAI sẽ tự động ẩn khỏi thanh bên PC, giúp người dùng không bao giờ click nhầm vào bản gốc.
4. **Đồng Bộ Hai Chiều An Toàn (Append-Only Sync):**
   - So khớp danh sách turn giữa bản gốc và bản clone `(ds)`.
   - Chỉ nối (append) các turn hội thoại mới mà không ghi đè hay xoá bất kỳ dữ liệu cũ nào.
   - Tự động tạo snapshot backup trước mỗi lần đồng bộ.
5. **Khả Năng Hoàn Tác (Rollback):**
   - Dễ dàng rollback phiên đã clone trở về trạng thái nguyên bản chỉ với một lệnh.

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
│   ├── db.py                  # Thao tác SQLite, tra cứu & ánh xạ cặp session
│   ├── migration.py           # Logic clone session & rollback an toàn
│   ├── sync.py                # Thuật toán đồng bộ hai chiều append-only
│   ├── focus.py               # Quản lý ẩn/hiện session theo mode (Focus Mode)
│   └── rollout.py             # Parser & Serializer cho định dạng JSONL Rollout wire
├── scripts/                   # Các script tiện ích
│   ├── focus-deepseek.bat     # 1-click chuyển PC sang chế độ DeepSeek
│   ├── focus-openai.bat       # 1-click chuyển PC về OpenAI
│   └── sync-sessions.bat      # 1-click đồng bộ toàn bộ session
└── tests/
    └── test_smoke.py          # Kiểm thử smoke test các module
```

---

## 🚀 Hướng Dẫn Sử Dụng

### 1. Liệt kê các session hiện có
```bash
python codex_migrator.py list
```

### 2. Chuyển đổi chế độ hiển thị trên PC (Focus Mode)
- **Ẩn session gốc OpenAI, chỉ hiện bản DeepSeek (ds):**
  ```bash
  python codex_migrator.py focus deepseek
  ```
- **Hiện lại session gốc OpenAI, ẩn bản DeepSeek:**
  ```bash
  python codex_migrator.py focus openai
  ```
- **Hiện toàn bộ:**
  ```bash
  python codex_migrator.py focus all
  ```

### 3. Đồng bộ hai chiều giữa PC và Điện thoại (Sync)
- **Đồng bộ tất cả các cặp đã ghép đôi:**
  ```bash
  python codex_migrator.py sync all
  ```
- **Đồng bộ một cặp cụ thể:**
  ```bash
  python codex_migrator.py sync Grok
  ```

### 4. Tạo bản clone mới sang DeepSeek
```bash
python codex_migrator.py migrate <THREAD_ID_HOẶC_TÊN> deepseek
```

### 5. Hoàn tác phiên clone
```bash
python codex_migrator.py rollback [THREAD_ID]
```

---

## 🌐 Chạy Trên Server Từ Xa (Linux / macOS)

Chỉ cần sao chép thư mục hoặc clone git lên server, sau đó chạy trực tiếp:
```bash
git clone <repo_url> codex-session-manager
cd codex-session-manager
python3 codex_migrator.py list
```
Hoặc chỉ định thư mục Codex:
```bash
python3 codex_migrator.py --codex-home /custom/path/.codex list
```
