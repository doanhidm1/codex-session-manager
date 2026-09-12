import os
import re
import shutil
from .config import CodexPaths

def read_config_toml(config_path):
    """Read config.toml content safely."""
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None

def update_config_toml(codex_home, updates):
    """
    Safely update top-level key-values in config.toml without modifying sections,
    maintaining comments and creating a pre-write backup copy.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.config_toml):
        return False

    bak_path = paths.config_toml + ".bak"
    try:
        shutil.copy2(paths.config_toml, bak_path)
    except Exception:
        pass

    text = read_config_toml(paths.config_toml)
    if text is None:
        return False

    # Find boundary before first section header [
    m = re.search(r'(?m)^\[', text)
    top_end = m.start() if m else len(text)
    top_part = text[:top_end]
    rest = text[top_end:]

    for k, v in updates.items():
        pattern = rf'(?m)^{re.escape(k)}\s*=.*$'
        val_str = f'"{v}"' if isinstance(v, str) else str(v)
        replacement = f'{k} = {val_str}'
        if re.search(pattern, top_part):
            top_part = re.sub(pattern, replacement, top_part)
        else:
            top_part = f"{replacement}\n" + top_part

    with open(paths.config_toml, "w", encoding="utf-8") as f:
        f.write(top_part + rest)

    return True
