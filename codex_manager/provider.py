import os
import re
import sqlite3

from .config import CodexPaths
from .toml_utils import update_config_toml

SUPPORTED_PROVIDERS = ("openai", "deepseek")
DEEPSEEK_DOCS_URL = "https://api-docs.deepseek.com/quick_start/agent_integrations/codex/"

DEFAULT_MODELS = {"deepseek": ("deepseek-flash", "high"), "openai": ("gpt-5.6-terra", "high")}


def is_supported_provider(provider):
    """Check if the given provider is supported."""
    if not provider:
        return False
    return provider.lower().strip() in SUPPORTED_PROVIDERS


def assert_supported_provider(provider):
    """Raise ValueError if the provider is unsupported."""
    if not is_supported_provider(provider):
        supported_str = ", ".join(repr(p) for p in SUPPORTED_PROVIDERS)
        raise ValueError(
            f"Provider '{provider}' is not supported. "
            f"Currently, this tool only supports switching between {supported_str}."
        )


def check_deepseek_config(codex_home):
    """
    Validate whether DeepSeek integration is configured in ~/.codex/config.toml.
    Returns (is_configured: bool, message: str).
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.config_toml):
        return False, f"config.toml not found at: {paths.config_toml}"

    try:
        with open(paths.config_toml, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return False, f"Unable to read config.toml: {e}"

    if not re.search(r"(?m)^\s*\[model_providers\.deepseek\]", content):
        return False, "Missing [model_providers.deepseek] section in config.toml"

    m = re.search(r"(?m)^\s*\[model_providers\.deepseek\](.*?)(?=^\s*\[|\Z)", content, re.DOTALL)
    if not m:
        return False, "Could not parse [model_providers.deepseek] block in config.toml"

    sec = m.group(1)
    token_match = re.search(r'(?m)^\s*experimental_bearer_token\s*=\s*"([^"]+)"', sec)
    if not token_match or not token_match.group(1).strip() or token_match.group(1).startswith("<"):
        return False, "Missing or placeholder experimental_bearer_token in [model_providers.deepseek]"

    return True, "DeepSeek configuration is valid."


def warn_if_deepseek_unconfigured(codex_home):
    """Print an alert and setup guide if DeepSeek is not configured."""
    ok, reason = check_deepseek_config(codex_home)
    if not ok:
        paths = CodexPaths(codex_home)
        print("\n" + "=" * 76)
        print(" [!] NOTICE: DeepSeek API provider is not configured in your Codex config.toml!")
        print(f"     Reason: {reason}")
        print(f"     Official Setup Guide: {DEEPSEEK_DOCS_URL}")
        print("\n     To configure DeepSeek in Codex, append this block to your config.toml:")
        print(f"     Config file: {paths.config_toml}")
        print("     --------------------------------------------------------------------")
        print("     [model_providers.deepseek]")
        print('     name = "deepseek"')
        print('     base_url = "https://api.deepseek.com/"')
        print('     wire_api = "responses"')
        print('     experimental_bearer_token = "<YOUR_DEEPSEEK_API_KEY>"')
        print("     --------------------------------------------------------------------")
        print("=" * 76 + "\n")
    return ok


def get_last_provider_settings(mapping_db_path, provider):
    """Fetch the last saved (model, reasoning_effort) for a provider from session_manager.sqlite."""
    prov = provider.lower().strip()
    def_model, def_effort = DEFAULT_MODELS.get(prov, ("deepseek-flash", "high"))
    if not os.path.exists(mapping_db_path):
        return def_model, def_effort

    try:
        with sqlite3.connect(mapping_db_path, timeout=5.0) as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS manager_settings (key TEXT PRIMARY KEY, value TEXT)")
            m_row = cur.execute("SELECT value FROM manager_settings WHERE key = ?", (f"last_{prov}_model",)).fetchone()
            e_row = cur.execute(
                "SELECT value FROM manager_settings WHERE key = ?", (f"last_{prov}_reasoning_effort",)
            ).fetchone()
            model = (m_row[0] if m_row and m_row[0] else None) or def_model
            effort = (e_row[0] if e_row and e_row[0] else None) or def_effort
            return model, effort
    except Exception:
        return def_model, def_effort


def save_provider_settings(mapping_db_path, provider, model, reasoning_effort):
    """Persist the last used (model, reasoning_effort) for a provider in session_manager.sqlite."""
    prov = provider.lower().strip()
    try:
        with sqlite3.connect(mapping_db_path, timeout=5.0) as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS manager_settings (key TEXT PRIMARY KEY, value TEXT)")
            if model:
                cur.execute(
                    "INSERT OR REPLACE INTO manager_settings (key, value) VALUES (?, ?)",
                    (f"last_{prov}_model", str(model)),
                )
            if reasoning_effort:
                cur.execute(
                    "INSERT OR REPLACE INTO manager_settings (key, value) VALUES (?, ?)",
                    (f"last_{prov}_reasoning_effort", str(reasoning_effort)),
                )
            conn.commit()
    except Exception:
        pass


def detect_current_provider_settings(codex_home, provider):
    """
    Detect the most recent model & reasoning effort used for this provider,
    checking recently updated threads in state_5.sqlite and config.toml.
    """
    paths = CodexPaths(codex_home)
    prov = provider.lower().strip()
    def_model, def_effort = DEFAULT_MODELS.get(prov, ("deepseek-flash", "high"))

    detected_model = None
    detected_effort = None

    # 1. Check state_5.sqlite threads for this provider
    if os.path.exists(paths.state_db):
        try:
            with sqlite3.connect(paths.state_db, timeout=5.0) as conn:
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT model, reasoning_effort FROM threads "
                    "WHERE model_provider = ? AND archived = 0 "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (prov,),
                ).fetchone()
                if not row:
                    row = cur.execute(
                        "SELECT model, reasoning_effort FROM threads "
                        "WHERE model_provider = ? ORDER BY updated_at DESC LIMIT 1",
                        (prov,),
                    ).fetchone()
                if row:
                    detected_model = row[0]
                    detected_effort = row[1]
        except Exception:
            pass

    # 2. Check config.toml if currently active on this provider
    if os.path.exists(paths.config_toml):
        try:
            with open(paths.config_toml, "r", encoding="utf-8") as f:
                content = f.read()
            prov_m = re.search(r'(?m)^model_provider\s*=\s*"([^"]+)"', content)
            active_prov = prov_m.group(1).lower() if prov_m else "openai"
            if active_prov == prov:
                m_match = re.search(r'(?m)^model\s*=\s*"([^"]+)"', content)
                e_match = re.search(r'(?m)^model_reasoning_effort\s*=\s*"([^"]+)"', content)
                if m_match and not detected_model:
                    detected_model = m_match.group(1)
                if e_match and not detected_effort:
                    detected_effort = e_match.group(1)
        except Exception:
            pass

    return detected_model or def_model, detected_effort or def_effort


def switch_provider_settings(codex_home, target_provider):
    """
    Saves outgoing provider settings and restores target provider settings in config.toml.
    Returns (restored_model, restored_reasoning_effort).
    """
    paths = CodexPaths(codex_home)
    target_prov = target_provider.lower().strip()

    # Identify outgoing provider from config.toml
    outgoing_prov = "openai"
    if os.path.exists(paths.config_toml):
        try:
            with open(paths.config_toml, "r", encoding="utf-8") as f:
                c = f.read()
            m = re.search(r'(?m)^model_provider\s*=\s*"([^"]+)"', c)
            if m:
                outgoing_prov = m.group(1).lower().strip()
        except Exception:
            pass

    # 1. Save outgoing provider's last settings
    if outgoing_prov in SUPPORTED_PROVIDERS and outgoing_prov != target_prov:
        out_model, out_effort = detect_current_provider_settings(codex_home, outgoing_prov)
        save_provider_settings(paths.mapping_db, outgoing_prov, out_model, out_effort)

    # 2. Fetch target provider's last settings to restore
    target_model, target_effort = get_last_provider_settings(paths.mapping_db, target_prov)
    if not target_model:
        target_model, target_effort = detect_current_provider_settings(codex_home, target_prov)
    if target_prov == "deepseek" and target_effort not in ("low", "high", "max"):
        target_effort = "high"

    # 3. Update config.toml
    update_config_toml(
        codex_home, {"model_provider": target_prov, "model": target_model, "model_reasoning_effort": target_effort}
    )

    return target_model, target_effort
