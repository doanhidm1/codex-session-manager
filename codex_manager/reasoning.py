import json
import os
import tempfile

from .config import CodexPaths

DEFAULT_ENABLED_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra", "persistent"]
REQUIRED_DEEPSEEK_EFFORTS = ("low", "high", "max")
OFFICIAL_DEEPSEEK_LEVELS = [
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "high", "description": "Extra high reasoning depth for complex problems"},
    {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
]


def ensure_global_state_reasoning_efforts(codex_home=None):
    """Ensure that Codex Desktop's electron-persisted-atom-state includes 'max', 'high', 'low'.

    If a user previously unticked 'max' in the Settings UI (or if it defaulted without 'max'),
    Codex Desktop will silently filter out 'max' from DeepSeek's supported reasoning levels.
    This function guarantees 'max', 'high', and 'low' are always preserved in enabled-reasoning-efforts.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.global_state):
        return False, "Global state file does not exist"

    try:
        with open(paths.global_state, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"Failed to read global state: {e}"

    atoms = data.get("electron-persisted-atom-state")
    if not isinstance(atoms, dict):
        atoms = {}
        data["electron-persisted-atom-state"] = atoms

    current_efforts = atoms.get("enabled-reasoning-efforts")
    updated = False

    if not isinstance(current_efforts, list):
        atoms["enabled-reasoning-efforts"] = list(DEFAULT_ENABLED_EFFORTS)
        updated = True
    else:
        efforts_set = set(current_efforts)
        for req in REQUIRED_DEEPSEEK_EFFORTS:
            if req not in efforts_set:
                current_efforts.append(req)
                updated = True

    if updated:
        dirname = os.path.dirname(paths.global_state)
        temp_fd, temp_path = tempfile.mkstemp(prefix="global-state-", suffix=".tmp", dir=dirname)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, paths.global_state)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False, f"Failed to save global state: {e}"

    return True, "Reasoning efforts verified in global state"


def ensure_deepseek_models_catalog(codex_home=None):
    """Ensure DeepSeek models in models.json only declare [low, high, max]

    without medium or missing levels, strictly adhering to DeepSeek's official specification.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.models_json):
        return False, "models.json does not exist"

    try:
        with open(paths.models_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"Failed to read models.json: {e}"

    models = data.get("models")
    if not isinstance(models, list):
        return False, "models.json has invalid structure"

    updated = False
    for model in models:
        slug = model.get("slug", "")
        if "deepseek" in slug.lower():
            current_levels = model.get("supported_reasoning_levels", [])
            efforts = [lvl.get("effort") for lvl in current_levels if isinstance(lvl, dict)]
            if efforts != list(REQUIRED_DEEPSEEK_EFFORTS) or model.get("default_reasoning_level") != "high":
                model["default_reasoning_level"] = "high"
                model["supported_reasoning_levels"] = list(OFFICIAL_DEEPSEEK_LEVELS)
                updated = True

    if updated:
        dirname = os.path.dirname(paths.models_json)
        temp_fd, temp_path = tempfile.mkstemp(prefix="models-", suffix=".tmp", dir=dirname)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, paths.models_json)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False, f"Failed to save models.json: {e}"

    return True, "DeepSeek models catalog verified"


def ensure_reasoning_efforts(codex_home=None, verbose=True):
    """Composite function to ensure both global state (UI atom) and models catalog

    guarantee low, high, and max reasoning effort levels.
    """
    ok1, msg1 = ensure_global_state_reasoning_efforts(codex_home)
    ok2, msg2 = ensure_deepseek_models_catalog(codex_home)
    if verbose:
        if ok1:
            print("[+] Ensured reasoning efforts in Codex Desktop global state (low, high, max enabled).")
        if ok2:
            print("[+] Verified DeepSeek catalog in models.json: low, high, max.")
    return ok1 and ok2


def get_reasoning_status(codex_home=None):
    """Inspect reasoning effort configuration status for diagnostics (doctor/check)."""
    paths = CodexPaths(codex_home)
    global_ok = False
    global_efforts = []
    if os.path.exists(paths.global_state):
        try:
            with open(paths.global_state, "r", encoding="utf-8") as f:
                d = json.load(f)
            atoms = d.get("electron-persisted-atom-state", {})
            efforts = atoms.get("enabled-reasoning-efforts")
            if isinstance(efforts, list):
                global_efforts = efforts
                global_ok = all(req in efforts for req in REQUIRED_DEEPSEEK_EFFORTS)
        except Exception:
            pass

    catalog_ok = False
    deepseek_models = []
    if os.path.exists(paths.models_json):
        try:
            with open(paths.models_json, "r", encoding="utf-8") as f:
                d = json.load(f)
            for m in d.get("models", []):
                slug = m.get("slug", "")
                if "deepseek" in slug.lower():
                    efforts = [x.get("effort") for x in m.get("supported_reasoning_levels", []) if isinstance(x, dict)]
                    deepseek_models.append((slug, efforts, m.get("default_reasoning_level")))
            catalog_ok = bool(
                deepseek_models and all(eff == list(REQUIRED_DEEPSEEK_EFFORTS) for _, eff, _ in deepseek_models)
            )
        except Exception:
            pass

    return {
        "global_ok": global_ok,
        "global_efforts": global_efforts,
        "catalog_ok": catalog_ok,
        "deepseek_models": deepseek_models,
    }
