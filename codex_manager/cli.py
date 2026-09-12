import sys

from .config import CodexPaths, get_default_codex_home
from .db import list_threads, resolve_thread
from .mapping import list_pairs_table, register_pair, remove_pair
from .migration import migrate_thread, rollback_thread
from .switch import switch_provider
from .sync import sync_all_pairs, sync_threads


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
    running, active                Detect if any Codex session is currently executing/generating
    pairs                          List all mapped session pairs from mapping database
    pair add <oai> <ds> [name]     Register a new session pair in mapping database
    pair remove <name|id>          Remove a session pair from mapping database
    check, doctor                  Diagnose environment, active sessions, and preserved settings
    list                           List recent threads in state_5.sqlite
    migrate <id> [provider]        Clone a session to target provider (default: deepseek)
    rollback [id]                  Undo a cloned session from latest backup snapshot

Global options:
    --codex-home <path>            Specify custom .codex home directory (auto-detected by default)
    --no-sync                      Skip automatic sync step during switch
    --force                        Rebuild broken/deleted target via full convert (rejects if Codex busy)
    --overwrite                    Full convert overwrite: rebuild target cleanly from source
    --help, -h                     Show this help message
""")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = list(sys.argv[1:])
    codex_home = None
    no_sync = False
    force = False

    if "--help" in args or "-h" in args:
        print_help()
        return 0

    if "--no-sync" in args:
        args.remove("--no-sync")
        no_sync = True

    if "--force" in args:
        args.remove("--force")
        force = True

    overwrite = False
    if "--overwrite" in args:
        args.remove("--overwrite")
        overwrite = True

    if "--codex-home" in args:
        idx = args.index("--codex-home")
        if idx + 1 < len(args):
            codex_home = args[idx + 1]
            del args[idx : idx + 2]

    if not codex_home:
        codex_home = get_default_codex_home()

    paths = CodexPaths(codex_home)
    cmd = args[0].lower() if len(args) > 0 else "list"

    if cmd == "list":
        list_threads(paths.state_db)
    elif cmd == "switch":
        mode = args[1] if len(args) > 1 else "deepseek"
        switch_provider(mode, codex_home, auto_sync=(not no_sync), force=force)
    elif cmd in ("running", "active"):
        from .activity import print_running_status

        print_running_status(codex_home)
    elif cmd == "pairs":
        list_pairs_table(codex_home)
    elif cmd == "pair":
        subcmd = args[1].lower() if len(args) > 1 else "list"
        if subcmd == "list":
            list_pairs_table(codex_home)
        elif subcmd == "add":
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
        elif subcmd in ("remove", "delete", "rm"):
            if len(args) < 3:
                print("Usage: python codex_migrator.py pair remove <name_or_id>")
                return 1
            if remove_pair(paths.mapping_db, args[2]):
                print(f"[+] Successfully removed pair [{args[2]}] from mapping database.")
            else:
                print(f"[!] No matching pair found to remove: {args[2]}")
    elif cmd == "sync":
        if overwrite:
            from .sync_overwrite import overwrite_all_mapped_pairs, overwrite_target_from_source

            if len(args) < 2 or args[1].lower() == "all":
                overwrite_all_mapped_pairs(codex_home, force=force)
            else:
                s_arg = args[1]
                t_arg = args[2] if len(args) > 2 else ""
                if not t_arg:
                    t_arg = (
                        s_arg.lower().replace("(ds)", "").strip()
                        if "(ds)" in s_arg.lower()
                        else f"{s_arg.strip()} (ds)"
                    )
                overwrite_target_from_source(s_arg, t_arg, codex_home, force=force)
        elif len(args) > 1 and args[1].lower() in ("openai", "deepseek"):
            switch_provider(args[1].lower(), codex_home, auto_sync=True, force=force)
        elif len(args) < 2 or args[1].lower() == "all":
            sync_all_pairs(codex_home, force=force)
        else:
            s_arg = args[1]
            t_arg = args[2] if len(args) > 2 else ""
            if not t_arg:
                t_arg = (
                    s_arg.lower().replace("(ds)", "").strip()
                    if "(ds)" in s_arg.lower()
                    else f"{s_arg.strip()} (ds)"
                )
            sync_threads(s_arg, t_arg, codex_home, force=force)
    elif cmd == "migrate":
        if len(args) < 2:
            print("Usage: python codex_migrator.py migrate <source_thread_id> [target_provider]")
            return 1
        s_id = args[1]
        tgt = args[2] if len(args) > 2 else None
        migrate_thread(s_id, tgt, codex_home)
    elif cmd in ("check", "doctor", "status"):
        import os

        from .activity import print_running_status
        from .provider import (
            DEEPSEEK_DOCS_URL,
            check_deepseek_config,
            get_last_provider_settings,
        )

        print("\n=== Codex Session Manager: Diagnostics & System Check ===")
        print(f"Codex Home : {paths.codex_home}")
        print(f"Config TOML: {paths.config_toml} (Exists: {os.path.exists(paths.config_toml)})")
        print(f"State DB   : {paths.state_db} (Exists: {os.path.exists(paths.state_db)})")
        print(f"History DB : {paths.th_db} (Exists: {os.path.exists(paths.th_db)})")
        print(f"Mapping DB : {paths.mapping_db} (Exists: {os.path.exists(paths.mapping_db)})")

        ds_ok, ds_reason = check_deepseek_config(codex_home)
        print("\n[DeepSeek API Configuration]")
        if ds_ok:
            print("  Status     : [OK] Configured and ready to use")
        else:
            print(f"  Status     : [WARNING] Not configured ({ds_reason})")
            print(f"  Setup Guide: {DEEPSEEK_DOCS_URL}")

        oai_m, oai_e = get_last_provider_settings(paths.mapping_db, "openai")
        ds_m, ds_e = get_last_provider_settings(paths.mapping_db, "deepseek")
        print("\n[Preserved Model Settings]")
        print(f"  OpenAI   : model='{oai_m}', reasoning_effort='{oai_e}'")
        print(f"  DeepSeek : model='{ds_m}', reasoning_effort='{ds_e}'")
        print_running_status(codex_home)
        print("=========================================================\n")
    elif cmd == "rollback":
        t_id = args[1] if len(args) > 1 else ""
        rollback_thread(t_id, codex_home)
    else:
        print(f"Unknown command: '{cmd}'")
        print_help()
        return 1

    return 0
