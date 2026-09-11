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

EXCLUDE_DIRS = {
    ".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules", ".claude",
}
EXCLUDE_FILES = {".env", ".DS_Store"}
# The whole of data/ is runtime state owned by the server (audit trail, the
# allowlist file, temp audio) — never code. Syncing it from the local repo's
# stale copy would silently clobber the live audit trail on every redeploy.
EXCLUDE_PATHS = {PROJECT_ROOT / "data"}


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


def run(
    client: paramiko.SSHClient, cmd: str, sudo_password: str | None = None
) -> tuple[int, str, str]:
    """Run a command, feeding `sudo -S` its password over stdin when asked.

    Over stdin rather than in the command string: an argument would be visible
    in `ps` on a shared machine for as long as the command runs.
    """
    stdin, stdout, stderr = client.exec_command(cmd)
    if sudo_password is not None:
        stdin.write(sudo_password + "\n")
        stdin.flush()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


#: Restarts through **systemd**, not `nohup ... &`.
#:
#: The service (`wa-voxo.service`, installed 2026-08-17) is what survives a
#: machine reboot — a reboot with the old `nohup` startup left the server down
#: for ~55 minutes with nobody aware. Once systemd owns port 8003, the old
#: `nohup` path silently stopped deploying anything: it killed whatever was in
#: `.run.pid` (nothing — systemd's PID is not there), started a second process
#: that died instantly on "address already in use", and then health-checked the
#: *old* process, which answered 200. A deploy that reports success while
#: running the previous code is the worst possible failure here, so this must
#: stay `systemctl restart`.
#:
#: The unit runs `tools/run_server.py`, not a bare `uvicorn ...` CLI
#: invocation: `logging_setup.configure()` is only ever called from inside that
#: script's `main()`, and calling `uvicorn` directly leaves the root logger at
#: WARNING, silently losing every `logger.info(...)` the /ops page shows.
REMOTE_RESTART_CMD = f"""
set -e
cd {REMOTE_DIR}
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -e ".[stt,server,agent,redis]" -q

# One sudo invocation, reading the password from stdin once — see `run`.
# The speech engine takes ~5s to warm up, so the health check waits.
sudo -S -p '' bash -c 'systemctl restart wa-voxo.service; sleep 15; \
  echo "PID: $(systemctl show -p MainPID --value wa-voxo.service)"; \
  systemctl is-active wa-voxo.service wa-voxo-tunnel.service'
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

    code, out, err = run(client, REMOTE_RESTART_CMD, sudo_password=load_env()["RACK_PASSWORD"])
    print(out)
    if err:
        print("STDERR:", err, file=sys.stderr)
    client.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
