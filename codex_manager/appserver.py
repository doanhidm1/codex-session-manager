"""Archive/unarchive threads through the Codex app-server (thread/archive, thread/unarchive).

Flipping threads.archived directly in state_5.sqlite is not enough: Codex's thread/list ignores it when a
modelProviders filter is used, so "(ds)" copies keep showing in the sidebar. The RPCs also move the rollout to
archived_sessions/ and fill archived_at, which is what the app honours.
"""

import json
import os
import select
import shutil
import subprocess
import time


def _app_server_binary(codex_home):
    cand = os.path.join(codex_home, "packages", "app-server-daemon", "current", "bin", "codex")
    if os.path.exists(cand):
        return cand
    return shutil.which("codex")


def set_threads_archived(codex_home, archive_ids=(), unarchive_ids=(), timeout=60):
    """Apply thread/archive and thread/unarchive. Returns the set of thread ids that were handled successfully."""
    archive_ids, unarchive_ids = list(archive_ids), list(unarchive_ids)
    done = set()
    if not archive_ids and not unarchive_ids:
        return done
    binary = _app_server_binary(codex_home)
    if not binary:
        return done
    env = dict(os.environ)
    env["CODEX_HOME"] = codex_home
    try:
        proc = subprocess.Popen(
            [binary, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
    except Exception:
        return done

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(req_id):
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([proc.stdout], [], [], 1)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == req_id:
                return msg
        return None

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "codex-session-manager", "version": "1"}},
            }
        )
        if recv(1) is None:
            return done
        send({"method": "initialized"})
        req_id = 2
        for method, ids in (("thread/archive", archive_ids), ("thread/unarchive", unarchive_ids)):
            for tid in ids:
                send({"id": req_id, "method": method, "params": {"threadId": tid}})
                resp = recv(req_id)
                if resp is not None and "error" not in resp:
                    done.add(tid)
                req_id += 1
    except Exception:
        pass
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    return done
