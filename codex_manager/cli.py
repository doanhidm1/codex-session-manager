import sys
from .config import get_default_codex_home, CodexPaths
from .db import list_threads
from .migration import migrate_thread, rollback_thread
from .focus import focus_mode
from .sync import sync_threads, sync_all_pairs

def print_help():
    print("""
Codex Session Manager - Multi-provider Session Management & Sync Tool
Version: 1.0.0 (Zero-dependency, Cross-Platform)

Cách sử dụng:
    python codex_migrator.py [--codex-home <path>] <lệnh> [tham số...]

Các lệnh hỗ trợ:
    list                         Liệt kê các session gần đây trong database
    migrate <id> [provider]      Tạo bản sao session sang provider mới (mặc định: deepseek)
    focus <deepseek|openai|all>  Bật/tắt ẩn session trên thanh bên PC để tránh chat nhầm
    sync all                     Đồng bộ hai chiều tất cả các cặp session (append-only)
    sync <source> [target]       Đồng bộ từ session nguồn sang session đích
    rollback [id]                Hoàn tác session đã clone từ bản backup gần nhất

Tuỳ chọn toàn cục:
    --codex-home <path>          Chỉ định thư mục .codex tuỳ chỉnh (mặc định tự nhận diện)
    --help, -h                   Hiển thị hướng dẫn này
""")

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    args = list(sys.argv[1:])
    codex_home = None

    if '--help' in args or '-h' in args:
        print_help()
        return 0

    if '--codex-home' in args:
        idx = args.index('--codex-home')
        if idx + 1 < len(args):
            codex_home = args[idx + 1]
            del args[idx:idx + 2]

    if not codex_home:
        codex_home = get_default_codex_home()

    paths = CodexPaths(codex_home)
    cmd = args[0].lower() if len(args) > 0 else 'list'

    if cmd == 'list':
        list_threads(paths.state_db)
    elif cmd == 'migrate':
        if len(args) < 2:
            print("Cách dùng: python codex_migrator.py migrate <source_thread_id> [target_provider]")
            return 1
        s_id = args[1]
        tgt = args[2] if len(args) > 2 else 'deepseek'
        migrate_thread(s_id, tgt, codex_home)
    elif cmd == 'rollback':
        t_id = args[1] if len(args) > 1 else ''
        rollback_thread(t_id, codex_home)
    elif cmd == 'focus':
        mode = args[1] if len(args) > 1 else 'deepseek'
        focus_mode(mode, codex_home)
    elif cmd == 'sync':
        if len(args) < 2 or args[1].lower() == 'all':
            sync_all_pairs(codex_home)
        else:
            s_arg = args[1]
            t_arg = args[2] if len(args) > 2 else ""
            if not t_arg:
                if "(ds)" in s_arg.lower():
                    t_arg = s_arg.lower().replace("(ds)", "").strip()
                else:
                    t_arg = f"{s_arg.strip()} (ds)"
            sync_threads(s_arg, t_arg, codex_home)
    else:
        print(f"Lệnh không xác định: '{cmd}'")
        print_help()
        return 1

    return 0
