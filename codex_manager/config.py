import os
import sys

def normalize_path(p):
    """Normalize file paths across Windows, macOS, and Linux."""
    if not p:
        return ""
    if isinstance(p, str) and p.startswith('\\\\?\\'):
        p = p[4:]
    return os.path.normpath(p)

def get_default_codex_home():
    """Dynamically resolve Codex home directory across all platforms."""
    env_home = os.environ.get("CODEX_HOME")
    if env_home and os.path.isdir(env_home):
        return normalize_path(os.path.abspath(env_home))
        
    default_home = os.path.expanduser("~/.codex")
    if os.path.isdir(default_home):
        return normalize_path(os.path.abspath(default_home))
        
    user_prof = os.environ.get("USERPROFILE")
    if user_prof:
        cand = os.path.join(user_prof, ".codex")
        if os.path.isdir(cand):
            return normalize_path(os.path.abspath(cand))
            
    return normalize_path(os.path.abspath(default_home))

class CodexPaths:
    def __init__(self, codex_home=None):
        self.codex_home = normalize_path(codex_home or get_default_codex_home())
        self.state_db = os.path.join(self.codex_home, "state_5.sqlite")
        self.th_db = os.path.join(self.codex_home, "thread_history_1.sqlite")
        self.cat_db = os.path.join(self.codex_home, "sqlite", "codex-dev.db")
        self.backup_root = os.path.join(self.codex_home, "backup-sessions")
        self.sessions_dir = os.path.join(self.codex_home, "sessions")
        self.mapping_db = os.path.join(self.codex_home, "session_manager.sqlite")
