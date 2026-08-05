#!/usr/bin/env python3
"""Push local changes to the rack server and restart the whatsapp-voice process.

Reads connection details (RACK_HOST, RACK_PORT, RACK_PASSWORD) from the local
.env, so there's nothing to type — just run:

    python tools/redeploy_rack.py

.env itself is NOT synced (it holds this script's own credentials, plus the
server may have hand-tuned values) — copy it up once by hand if it changes:

    scp -P 2222 .env kapil@122.176.147.78:/home/deepanshu/WA_VOXO/.env
"""
from __future__ import annotations

import posixpath
import sys
from pathlib import Path

import paramiko

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REMOTE_DIR = "/home/deepanshu/WA_VOXO"
PORT = 8003

EXCLUDE_DIRS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules", ".claude"}
EXCLUDE_FILES = {".env", ".DS_Store"}
EXCLUDE_PATHS = {PROJECT_ROOT / "data" / "audio"}


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def connect() -> paramiko.SSHClient:
    env = load_env()
    user, _, host = env["RACK_HOST"].partition("@")
    port = int(env.get("RACK_PORT", "22"))
    password = env["RACK_PASSWORD"]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=password, timeout=20)
    return client


def should_skip(local_path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in local_path.parts):
        return True
    if any(local_path == p or p in local_path.parents for p in EXCLUDE_PATHS):
        return True
    return local_path.name in EXCLUDE_FILES


_dir_cache: set[str] = set()


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir in _dir_cache:
        return
    parent = posixpath.dirname(remote_dir)
    if parent and parent != remote_dir:
        ensure_remote_dir(sftp, parent)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        sftp.mkdir(remote_dir)
    _dir_cache.add(remote_dir)


def upload_dir(sftp: paramiko.SFTPClient, local_root: Path, remote_root: str) -> int:
    count = 0
    for local_path in sorted(local_root.rglob("*")):
        if local_path.is_dir() or should_skip(local_path):
            continue
        rel = local_path.relative_to(local_root)
        remote_path = posixpath.join(remote_root, *rel.parts)
        ensure_remote_dir(sftp, posixpath.dirname(remote_path))
        sftp.put(str(local_path), remote_path)
        count += 1
    return count


def run(client: paramiko.SSHClient, cmd: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


REMOTE_RESTART_CMD = f"""
set -e
cd {REMOTE_DIR}
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -e ".[stt,server,agent]" -q

if [ -f .run.pid ]; then
  OLD_PID=$(cat .run.pid)
  if kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID"
    sleep 1
  fi
  rm -f .run.pid
fi

nohup .venv/bin/uvicorn whatsapp_voice.whatsapp.app:create_app \\
  --factory --host 0.0.0.0 --port {PORT} --workers 1 \\
  > uvicorn.log 2>&1 &
echo $! > .run.pid
sleep 2
echo "PID: $(cat .run.pid)"
curl -s localhost:{PORT}/health || echo "health check failed -- check uvicorn.log"
"""


def main() -> None:
    print(f"Connecting and syncing {PROJECT_ROOT} -> {REMOTE_DIR} ...")
    client = connect()
    sftp = client.open_sftp()
    ensure_remote_dir(sftp, REMOTE_DIR)
    n = upload_dir(sftp, PROJECT_ROOT, REMOTE_DIR)
    sftp.close()
    print(f"Uploaded {n} files. Installing deps and restarting service...")

    code, out, err = run(client, REMOTE_RESTART_CMD)
    print(out)
    if err:
        print("STDERR:", err, file=sys.stderr)
    client.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
