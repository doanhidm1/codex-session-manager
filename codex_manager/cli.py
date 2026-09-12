import sys
from .config import get_default_codex_home, CodexPaths
from .db import list_threads, resolve_thread
from .migration import migrate_thread, rollback_thread
from .switch import switch_provider
from .sync import sync_threads, sync_all_pairs
from .mapping import list_pairs_table, register_pair, remove_pair, auto_seed_existing_pairs

def print_help():
    print("""
Codex Session Manager - Multi-provider Session Management & Sync Tool
Version: 1.1.0 (Zero-dependency, Cross-Platform)

Usage:
    python codex_migrator.py [--codex-home <path>] <command> [arguments...]

Main commands:
    switch <deepseek|openai|all>   Auto-SYNC new messages and SWITCH active sidebar provider
    sync all                       Bidirectional sync of all registered session pairs (append-only)
    sync <source> [target]         Incrementally sync new turns from source to target
    pairs                          List all mapped session pairs from mapping database
    pair add <oai> <ds> [name]     Register a new session pair in mapping database
    pair remove <name|id>          Remove a session pair from mapping database
    list                           List recent threads in state_5.sqlite
    migrate <id> [provider]        Clone a session to target provider (default: deepseek)
    rollback [id]                  Undo a cloned session from latest backup snapshot

Global options:
    --codex-home <path>            Specify custom .codex home directory (auto-detected by default)
    --no-sync                      Skip automatic sync step during switch
    --help, -h                     Show this help message
""")

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    args = list(sys.argv[1:])
    codex_home = None
    no_sync = False

    if '--help' in args or '-h' in args:
        print_help()
        return 0

    if '--no-sync' in args:
        args.remove('--no-sync')
        no_sync = True

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
    elif cmd in ('switch', 'focus'):
        mode = args[1] if len(args) > 1 else 'deepseek'
        switch_provider(mode, codex_home, auto_sync=(not no_sync))
    elif cmd == 'pairs':
        list_pairs_table(codex_home)
    elif cmd == 'pair':
        subcmd = args[1].lower() if len(args) > 1 else 'list'
        if subcmd == 'list':
            list_pairs_table(codex_home)
        elif subcmd == 'add':
            if len(args) < 4:
                print("Usage: python codex_migrator.py pair add <openai_id_or_name> <deepseek_id_or_name> [custom_name]")
                return 1
            o_row = resolve_thread(args[2], codex_home)
            d_row = resolve_thread(args[3], codex_home)
            if not o_row:
                print(f"ERROR: OpenAI session not found: {args[2]}")
                return 1
            if not d_row:
                print(f"ERROR: DeepSeek session not found: {args[3]}")
                return 1
            p_name = args[4] if len(args) > 4 else (o_row[1] or o_row[2] or "Custom Pair")
            register_pair(paths.mapping_db, p_name, o_row[0], d_row[0])
            print(f"[+] Successfully registered pair: [{p_name}] ({o_row[0]} <---> {d_row[0]})")
        elif subcmd in ('remove', 'delete', 'rm'):
            if len(args) < 3:
                print("Usage: python codex_migrator.py pair remove <name_or_id>")
                return 1
            if remove_pair(paths.mapping_db, args[2]):
                print(f"[+] Successfully removed pair [{args[2]}] from mapping database.")
            else:
                print(f"[!] No matching pair found to remove: {args[2]}")
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
    elif cmd == 'migrate':
        if len(args) < 2:
            print("Usage: python codex_migrator.py migrate <source_thread_id> [target_provider]")
            return 1
        s_id = args[1]
        tgt = args[2] if len(args) > 2 else 'deepseek'
        migrate_thread(s_id, tgt, codex_home)
    elif cmd == 'rollback':
        t_id = args[1] if len(args) > 1 else ''
        rollback_thread(t_id, codex_home)
    else:
        print(f"Unknown command: '{cmd}'")
        print_help()
        return 1

    return 0
