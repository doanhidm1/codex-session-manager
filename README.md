# Codex Session Manager

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Dependencies: None](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()
[![Platform: Windows | macOS | Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A zero-dependency, cross-platform CLI tool and Python library for managing, migrating, and bidirectionally synchronizing conversational sessions between **OpenAI Codex** and **DeepSeek** (or other model providers).

---

### 🌟 Key Features

1. **Provider Switching with Automatic Incremental Sync & 1-Click Launchers:**
   - Switching providers (e.g. `switch deepseek` or `switch openai`) automatically syncs all newly generated conversation turns **before** toggling sidebar visibility.
   - Includes 1-click automation scripts (`scripts/switch-openai.bat`, `scripts/switch-deepseek.bat`) that combine turn synchronization, config updates, and sidebar toggling in a single click.
2. **1M Context Window & Compaction Threshold Protection:**
   - Automatically maintains `model_context_window = 1000000` and `model_auto_compact_token_limit = 900000` in `config.toml`.
   - Prevents OpenAI sessions from prematurely compacting at default lower thresholds (~588k tokens / 70%) and enables long multi-turn sessions (up to 1,000,000 tokens) to run smoothly.
3. **Model & Reasoning Effort Preservation:**
   - Automatically remembers and restores the exact model and reasoning effort settings last used for each provider (e.g. `gpt-5.6-terra` / `high` on OpenAI, `deepseek-flash` / `high` on DeepSeek).
4. **Automatic New Thread Discovery & Suffix Tagging:**
   - Automatically detects any new session created while working in DeepSeek (or OpenAI) mode when switching.
   - Clones a synchronized counterpart on the destination provider, pairs them in the mapping database, and tags the DeepSeek session name with `(ds)` for clear visual distinction.
5. **High-Fidelity Wire-Format Turn Extraction & Append-Only Sync:**
   - Reconstructs user messages, assistant responses, reasoning thoughts, tool execution IDs, and scheduled tasks with zero data loss.
   - Safe SQLite ordinal and byte-offset projection prevents history duplication or corrupted state.
6. **Dedicated Mapping Database (`session_manager.sqlite`):**
   - Explicitly persists mapped pairs `(openai_thread_id <---> deepseek_thread_id)` along with timestamps (`last_synced_at`). Eliminates brittle name matching.
7. **Safe Unarchiving (No Accidental Restorations):**
   - When switching provider modes, the manager **only** alters the archive status of thread IDs explicitly registered in the mapping database. Manually archived sessions remain untouched.
8. **DeepSeek Pre-flight Configuration Detection:**
   - Detects whether DeepSeek is configured in `~/.codex/config.toml` according to the [Official DeepSeek Codex Integration Guide](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/). Warns with clear instructions if missing.
9. **Active Running Session Protection:**
   - Detects whether Codex is actively generating a response or running tools (via `thread_history_1.sqlite` status `inProgress` and live rollout write timestamps).
   - Automatically halts `switch` and `sync` operations with a clear, detailed warning to prevent SQLite database locks (`database is locked`) or corrupted rollout logs (overridable with `--force`).
10. **Zero External Dependencies & Cross-Platform:**
   - Built 100% using the Python standard library (`sqlite3`, `json`, `uuid`, `time`, `os`, `shutil`, `sys`, `re`).
   - Seamlessly works across Windows (`%USERPROFILE%\.codex`), macOS (`~/.codex`), and Linux (`~/.codex`).

---

## 📂 Project Structure

```
codex-session-manager/
├── .gitignore
├── LICENSE
├── README.md                  # Comprehensive documentation
├── pyproject.toml             # Metadata, build config & Ruff settings
├── codex_migrator.py          # Unified entrypoint CLI script
├── codex_manager/             # Core Python package (Modular & clean <190 lines/file)
│   ├── __init__.py
│   ├── __main__.py            # Support for `python -m codex_manager`
│   ├── activity.py            # Active running session detection & lock protection
│   ├── backup.py              # Snapshot backup and rollback engine
│   ├── cli.py                 # Command dispatcher and argument parser
│   ├── config.py              # Environment and cross-platform path resolver
│   ├── db.py                  # SQLite adapters for state_5 and thread_history_1
│   ├── discovery.py           # Auto-discovery of unmapped threads & name tagging
│   ├── extractor.py           # Turn extraction & payload parsing from rollout files
│   ├── mapping.py             # Dedicated mapping database (session_manager.sqlite)
│   ├── migration.py           # Session clone orchestration and metadata preservation
│   ├── pair_health.py         # Target health verification & deleted/corrupt target recovery
│   ├── projection.py          # SQLite byte-offset and ordinal projection
│   ├── provider.py            # Provider validation & model settings preservation
│   ├── rollout.py             # Parser and serializer for JSONL wire format
│   ├── switch.py              # Provider switcher with auto-sync & archive toggle
│   ├── sync.py                # Incremental two-way append-only sync algorithm
│   ├── sync_builder.py        # High-fidelity payload builder for incremental sync
│   ├── sync_overwrite.py      # Full convert overwrite engine (wipes corrupt targets)
│   └── toml_utils.py          # Atomic TOML config reader and updater
├── scripts/                   # 1-Click Automation scripts (Windows, macOS, Linux)
│   ├── switch-deepseek.bat    # Windows: 1-click sync & switch to DeepSeek
│   ├── switch-openai.bat      # Windows: 1-click sync & switch to OpenAI
│   ├── switch-deepseek.sh     # macOS/Linux: 1-click sync & switch to DeepSeek
│   └── switch-openai.sh       # macOS/Linux: 1-click sync & switch to OpenAI
└── tests/
    └── test_smoke.py          # Fast automated test suite (< 5ms)
```

---

## ⚙️ Initial Setup: DeepSeek Configuration (First-Time Use)

If you are using DeepSeek with Codex for the first time or setting up a new environment, you must configure DeepSeek as a custom provider in your Codex configuration file according to the [Official DeepSeek Codex Integration Guide](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/).

### Step 1: Obtain a DeepSeek API Key
1. Sign up or log in at the [DeepSeek Platform](https://platform.deepseek.com/).
2. Navigate to **API Keys** and generate an API key (`sk-...`).

### Step 2: Configure `config.toml`
Locate your Codex configuration file:
- **Windows**: `%USERPROFILE%\.codex\config.toml` (typically `C:\Users\<YourUsername>\.codex\config.toml`)
- **macOS / Linux**: `~/.codex/config.toml`

Open `config.toml` in your text editor and append the following configuration block at the bottom:

```toml
[model_providers.deepseek]
name = "deepseek"
base_url = "https://api.deepseek.com/"
wire_api = "responses"
experimental_bearer_token = "<YOUR_DEEPSEEK_API_KEY>"
```

> **Note:** Replace `<YOUR_DEEPSEEK_API_KEY>` with your actual DeepSeek secret key (e.g. `sk-9aa0...`).

### Step 3: Run Pre-Flight Diagnostics
Validate your setup using the built-in doctor command:

```bash
python codex_migrator.py doctor
```

If configured correctly, the check confirms readiness:
```
=== Codex Session Manager: Diagnostics & System Check ===
Codex Home : C:\Users\<Username>\.codex
Config TOML: C:\Users\<Username>\.codex\config.toml (Exists: True)

[DeepSeek API Configuration]
  Status     : [OK] Configured and ready to use
```

If the block or token is missing, the doctor will display an alert banner with direct instructions.

---

## 🚀 Quick Start & CLI Usage

### 1-Click Scripts (Recommended for Daily Use)
On Windows, you can simply double-click the batch scripts in `scripts/`:
- **`scripts\switch-openai.bat`**: Syncs new DeepSeek turns to OpenAI, restores OpenAI model/reasoning settings, ensures 1M context limits, and shows OpenAI sessions in sidebar.
- **`scripts\switch-deepseek.bat`**: Syncs new OpenAI turns to DeepSeek, restores DeepSeek model/reasoning settings, ensures 1M context limits, and shows DeepSeek sessions in sidebar.

### Command-Line Interface (CLI)
You can also run commands via `python codex_migrator.py <command>` or `python -m codex_manager <command>`.

#### 1. View Registered Session Pairs
List all active mappings and their sync status:
```bash
python codex_migrator.py pairs
```

#### 2. Switch Provider (Auto-Syncs by Default)
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

#### 3. Manual Two-Way Synchronization
Synchronize conversation turns across sessions without toggling visibility:
```bash
# Sync all registered pairs
python codex_migrator.py sync all

# Sync a specific session pair by name or Thread ID
python codex_migrator.py sync Grok

# Full convert overwrite (if target is corrupted or hung, cleanly rebuilds target from source):
python codex_migrator.py sync all --overwrite
python codex_migrator.py sync Grok --overwrite
```

#### 4. Clone an Existing Session to DeepSeek
Duplicate an existing OpenAI thread into a clean DeepSeek session with full conversation history:
```bash
python codex_migrator.py migrate <THREAD_ID_OR_NAME> deepseek
```
*(The cloned thread is automatically registered in the mapping database.)*

#### 5. Managing Mappings
Add or remove session pairs in the mapping database manually:
```bash
# Register a pair
python codex_migrator.py pair add <openai_id_or_name> <deepseek_id_or_name> [optional_name]

# Unregister a pair
python codex_migrator.py pair remove <name_or_id>
```

#### 6. Diagnostics, Status, and Listing
```bash
# Detect if any Codex session is currently executing or generating
python codex_migrator.py running

# Run pre-flight checks (DeepSeek config, running sessions, database health, model settings)
python codex_migrator.py doctor

# List all threads across state_5.sqlite
python codex_migrator.py list

# Safe rollback to previous backup snapshot if needed
python codex_migrator.py rollback
```

#### 7. Safety, Lock Protection & `--force` Recovery
- **Lock & Active Turn Detection**: The system strictly checks that Codex is completely idle before allowing `switch` or `sync`. It verifies that:
  1. No turns are `inProgress` or generating.
  2. All SQLite databases (`state_5.sqlite`, `thread_history_1.sqlite`, `session_manager.sqlite`) are clean and lockable via `BEGIN EXCLUSIVE`.
  3. Active rollout file handles can be opened for writing.
  If Codex is actively writing or locking files, operations are unconditionally blocked.
- **`--force` Role (Target Recovery)**:
  `--force` is **NOT** for bypassing a running Codex process (which is unsafe and blocked). Instead, `--force` is specifically designed to recover when a mapped target session was deleted from `state_5.sqlite` or its rollout is corrupted:
  - If a mapped target is missing/broken and `--force` is omitted, the tool halts and warns the user.
  - If `--force` is passed, it cleanly rebuilds the target session from the source via full conversion from scratch.
  - Brand new unmapped sessions are discovered and paired automatically without needing `--force`.

---

## 🧪 Running Tests & Code Quality

### Unit Tests
The test suite runs with zero dependencies using Python's built-in `unittest` runner:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

All tests execute in milliseconds and cover config path resolution, database helper queries, mapping creation, provider switching filters, and target health verification.

### Linting & Formatting (Ruff)
This project uses [Ruff](https://github.com/astral-sh/ruff) for high-performance Python linting and code formatting, configured via `pyproject.toml`:

```bash
# Check code for lint and import errors
ruff check .

# Automatically apply safe fixes
ruff check --fix .

# Check formatting compliance
ruff format --check .
```

---

## 🤝 Contributing

Contributions are warmly welcome! When submitting pull requests:
1. Ensure the code adheres to the **Zero External Dependencies** rule (Python standard library only for runtime).
2. Ensure all lint checks pass (`ruff check .`).
3. Maintain cross-platform compatibility across Windows, Linux, and macOS.
4. Keep all files modular and maintainable (< 190 lines per file).
5. Add unit tests in `tests/` for any new functionality and ensure they pass.
6. Keep commit messages clear and descriptive.

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
