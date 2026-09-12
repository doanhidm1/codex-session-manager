# Codex Session Manager

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Dependencies: None](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()
[![Platform: Windows | macOS | Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

A zero-dependency, cross-platform CLI tool and Python library for managing, migrating, and bidirectionally synchronizing conversational sessions between **OpenAI Codex** and **DeepSeek** (or other model providers).

---

## 🌟 Key Features

1. **Provider Switching with Automatic Incremental Sync:**
   - Switching provider focus (e.g. `switch deepseek` or `switch openai`) automatically syncs all newly generated conversation turns **before** toggling sidebar visibility. You never have to remember to run manual syncs!
2. **Dedicated Mapping Database (`session_manager.sqlite`):**
   - Explicitly persists mapped pairs `(openai_thread_id <---> deepseek_thread_id)` along with timestamps (`last_synced_at`).
   - Eliminates ambiguity and brittle name matching.
3. **Safe Unarchiving (No Accidental Restorations):**
   - When switching provider modes, the manager **only** alters the archive status of thread IDs explicitly registered in the mapping database.
   - Any older sessions or abandoned projects that you manually archived remain 100% archived and undisturbed.
4. **Non-Destructive Append-Only Two-Way Sync:**
   - Inspects and reconciles turn identifiers across paired sessions.
   - Only appends missing conversation turns. Existing turns are never overwritten, modified, or truncated.
   - Automatically generates timestamped backup snapshots before performing any modifications.
5. **Zero External Dependencies:**
   - Built entirely using the Python 3 standard library (`sqlite3`, `json`, `uuid`, `time`, `os`, `shutil`, `sys`, `re`).
   - Requires no `pip install` or external wheel compilation. Works immediately on any standard Python 3 installation.
6. **Cross-Platform Compatibility:**
   - Seamlessly resolves Codex data directories across Windows (`%USERPROFILE%\.codex`), macOS (`~/.codex`), and Linux (`~/.codex`), with support for custom `CODEX_HOME` environment variables.

---

## 📂 Project Structure

```
codex-session-manager/
├── .gitignore
├── LICENSE
├── README.md                  # Comprehensive documentation
├── codex_migrator.py          # Unified entrypoint CLI script
├── codex_manager/             # Core Python package
│   ├── __init__.py
│   ├── __main__.py            # Support for `python -m codex_manager`
│   ├── cli.py                 # Command dispatcher and argument parser
│   ├── config.py              # Environment and cross-platform path resolver
│   ├── db.py                  # SQLite adapters for state_5 and thread_history_1
│   ├── mapping.py             # Dedicated mapping database (session_manager.sqlite)
│   ├── backup.py              # Snapshot backup and rollback engine
│   ├── projection.py          # SQLite byte-offset and ordinal projection
│   ├── rollout.py             # Parser and serializer for JSONL wire format
│   ├── migration.py           # Session clone orchestration and metadata preservation
│   ├── sync.py                # Incremental two-way append-only sync algorithm
│   └── switch.py              # Provider switcher with auto-sync & archive toggle
├── scripts/                   # Helper automation scripts
│   ├── switch-deepseek.bat    # 1-click auto-sync + switch to DeepSeek
│   ├── switch-openai.bat      # 1-click auto-sync + switch to OpenAI
│   └── sync-sessions.bat      # 1-click bidirectional sync
└── tests/
    └── test_smoke.py          # Fast automated test suite (< 5ms)
```

---

## 🚀 Quick Start & CLI Usage

You can run commands either via `python codex_migrator.py <command>` or `python -m codex_manager <command>`.

### 1. View Registered Session Pairs
List all active mappings and their sync status:
```bash
python codex_migrator.py pairs
```

### 2. Switch Provider Focus (Auto-Syncs by Default)
- **Switch to DeepSeek** (Auto-syncs new turns, displays DeepSeek sessions, hides OpenAI sessions):
  ```bash
  python codex_migrator.py switch deepseek
  ```
- **Switch to OpenAI** (Auto-syncs new turns, displays OpenAI sessions, hides DeepSeek sessions):
  ```bash
  python codex_migrator.py switch openai
  ```
- **Display all sessions** (Unhides both OpenAI and DeepSeek pairs):
  ```bash
  python codex_migrator.py switch all
  ```
> **Tip:** Pass `--no-sync` if you wish to toggle the sidebar without syncing turns:
> ```bash
> python codex_migrator.py switch deepseek --no-sync
> ```

### 3. Manual Two-Way Synchronization
Synchronize conversation turns across sessions without toggling visibility:
```bash
# Sync all registered pairs
python codex_migrator.py sync all

# Sync a specific session pair by name or Thread ID
python codex_migrator.py sync Grok
```

### 4. Clone an Existing Session to DeepSeek
Duplicate an existing OpenAI thread into a clean DeepSeek session with full conversation history:
```bash
python codex_migrator.py migrate <THREAD_ID_OR_NAME> deepseek
```
*(The cloned thread is automatically registered in the mapping database.)*

### 5. Managing Mappings
Add or remove session pairs in the mapping database manually:
```bash
# Register a pair
python codex_migrator.py pair add <openai_id_or_name> <deepseek_id_or_name> [optional_name]

# Unregister a pair
python codex_migrator.py pair remove <name_or_id>
```

### 6. Listing and Diagnostics
```bash
# List all threads across state_5.sqlite
python codex_migrator.py list

# Safe rollback to previous backup snapshot if needed
python codex_migrator.py rollback
```

---

## 🧪 Running Tests

The test suite runs with zero dependencies using Python's built-in `unittest` runner:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

All tests execute in milliseconds and cover config path resolution, database helper queries, mapping creation, and provider switching filters.

---

## 🤝 Contributing

Contributions are warmly welcome! When submitting pull requests:
1. Ensure the code adheres to the **Zero External Dependencies** rule (Python standard library only).
2. Maintain cross-platform compatibility across Windows, Linux, and macOS.
3. Add unit tests in `tests/` for any new functionality.
4. Keep commit messages clear and descriptive.

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
