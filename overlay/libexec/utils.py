# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import os
import sys
import shlex
import shutil
import base64
import tempfile
import subprocess
import configparser
import argparse
import stat
import pwd
import grp
import time

CLIENT_ROOT = os.environ.get("SENSOS_CLIENT_ROOT", "/sensos")
CLIENT_API_USERNAME = "sensos"

API_PASSWORD_FILE = os.path.join(CLIENT_ROOT, "keys", "api_password")
DEFAULTS_CONF = os.path.join(CLIENT_ROOT, "etc", "defaults.conf")
NETWORK_CONF = os.path.join(CLIENT_ROOT, "etc", "network.conf")
INSTALL_STATE_FILE = os.path.join(CLIENT_ROOT, "etc", "install-state.env")
LOG_DIR = os.path.join(CLIENT_ROOT, "log")
DEFAULT_PORT = "8765"
HEALTHZ_PATH = "/healthz"


def require_requests():
    try:
        import requests
    except ImportError as exc:
        sys.exit("Error: Python package 'requests' is required for this operation.")
    return requests


def require_dir(path: str, name: str):
    if not os.path.isdir(path):
        sys.exit(f"Error: required directory not found: {name} ({path})")


def require_cmd(cmd: str):
    if shutil.which(cmd) is None:
        sys.exit(f"Error: required command not found in PATH: {cmd}")


def _current_username() -> str:
    return os.environ.get("USER") or subprocess.run(
        ["id", "-un"], text=True, capture_output=True, check=False
    ).stdout.strip()


def _help_only_invocation(argv: list[str]) -> bool:
    return bool(argv) and all(arg in ("-h", "--help", "help") for arg in argv)


def ensure_sensos_admin(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    if os.geteuid() == 0 or _current_username() == "sensos-admin":
        return

    if _help_only_invocation(argv):
        return

    if os.environ.get("SENSOS_ADMIN_REEXEC") == "1":
        sys.exit("Error: failed to re-run as sensos-admin.")

    script_path = os.path.realpath(sys.argv[0])
    preserve_env = ["SENSOS_CLIENT_ROOT", "SENSOS_ADMIN_REEXEC"]
    print("Re-running as sensos-admin...", file=sys.stderr)
    os.execvp(
        "sudo",
        [
            "sudo",
            f"--preserve-env={','.join(preserve_env)}",
            "-u",
            "sensos-admin",
            "env",
            "SENSOS_ADMIN_REEXEC=1",
            script_path,
            *argv,
        ],
    )


def require_nonempty(value, what: str):
    if value is None or (isinstance(value, str) and value.strip() == ""):
        sys.exit(f"Error: required value not set: {what}")
    return value


def privileged_shell(cmd, check=False, silent=False, user=None):
    is_root = os.geteuid() == 0
    if user:
        full_cmd = (
            f"sudo -u {user} {cmd}"
            if not is_root
            else f"su - {user} -c {shlex.quote(str(cmd))}"
        )
    else:
        full_cmd = cmd if is_root else f"sudo {cmd}"
    try:
        output = subprocess.check_output(full_cmd, shell=True, text=True).strip()
        return output, 0
    except subprocess.CalledProcessError as e:
        if not silent:
            print(f"❌ Command failed: {full_cmd}\n{e}", file=sys.stderr)
        if check:
            raise
        return None, e.returncode
    except Exception as e:
        if not silent:
            print(f"❌ Error running {full_cmd}: {e}", file=sys.stderr)
        if check:
            raise
        return None, 1


def remove_file(path):
    privileged_shell(f"rm -f {shlex.quote(str(path))}", silent=True)


def create_dir(path, owner="root", group=None, mode=0o755):
    group = group or owner
    if not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            privileged_shell(f"mkdir -p {shlex.quote(str(path))}", silent=True)
    try:
        st = os.stat(path)
        current_mode = stat.S_IMODE(st.st_mode)
        desired_uid = pwd.getpwnam(owner).pw_uid
        desired_gid = grp.getgrnam(group).gr_gid
        if current_mode != mode:
            privileged_shell(f"chmod {oct(mode)[2:]} {shlex.quote(str(path))}", silent=True)
        if st.st_uid != desired_uid or st.st_gid != desired_gid:
            privileged_shell(f"chown {owner}:{group} {shlex.quote(str(path))}", silent=True)
    except Exception:
        privileged_shell(f"chmod {oct(mode)[2:]} {shlex.quote(str(path))}", silent=True)
        privileged_shell(f"chown {owner}:{group} {shlex.quote(str(path))}", silent=True)


def read_file(filepath):
    output, rc = privileged_shell(f"cat {shlex.quote(str(filepath))}", silent=True)
    return output.strip() if output else None


def write_file(filepath, content, mode=0o644, user="root", group=None):
    group = group or user
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        os.replace(tmp_path, filepath)
    except Exception:
        privileged_shell(
            f"mv {shlex.quote(tmp_path)} {shlex.quote(str(filepath))}", silent=True
        )
        tmp_path = None
    else:
        tmp_path = None

    try:
        os.chmod(filepath, mode)
    except Exception:
        privileged_shell(f"chmod {oct(mode)[2:]} {shlex.quote(str(filepath))}", silent=True)

    try:
        desired_uid = pwd.getpwnam(user).pw_uid
        desired_gid = grp.getgrnam(group).gr_gid
        st = os.stat(filepath)
        if st.st_uid != desired_uid or st.st_gid != desired_gid:
            os.chown(filepath, desired_uid, desired_gid)
    except Exception:
        privileged_shell(f"chown {user}:{group} {shlex.quote(str(filepath))}", silent=True)


def set_permissions_and_owner(
    path: str, mode: int, user: str = None, group: str = None
):
    group = group or user
    privileged_shell(f"chmod {oct(mode)[2:]} {shlex.quote(str(path))}", silent=True)
    if user:
        privileged_shell(f"chown {user}:{group} {shlex.quote(str(path))}", silent=True)


def get_basic_auth(api_password, username=CLIENT_API_USERNAME):
    return base64.b64encode(f"{username}:{api_password}".encode()).decode()


def build_basic_auth_header(api_password, username=CLIENT_API_USERNAME):
    return {"Authorization": f"Basic {get_basic_auth(api_password, username=username)}"}


def healthz_url(config_server, port):
    return f"http://{config_server}:{port}{HEALTHZ_PATH}"


def network_info_url(config_server, port, network_name):
    return f"http://{config_server}:{port}/api/v1/client/networks/{network_name}"


def get_server_health(config_server, port, timeout=3):
    requests = require_requests()
    url = healthz_url(config_server, port)
    try:
        response = requests.get(url, timeout=timeout)
        payload = {}
        try:
            payload = response.json()
        except Exception:
            payload = {}

        status = payload.get("status")
        if response.status_code == 200 and status == "ok":
            return {"reachable": True, "ready": True, "status": status}
        if response.status_code == 503 and status == "starting":
            return {"reachable": True, "ready": False, "status": status}
        return {
            "reachable": True,
            "ready": response.status_code == 200,
            "status": status or f"http-{response.status_code}",
        }
    except requests.exceptions.ConnectionError:
        return {"reachable": False, "ready": False, "status": "unreachable"}
    except Exception as e:
        print(f"⚠️ Unexpected error when checking server health: {e}", file=sys.stderr)
        return {"reachable": False, "ready": False, "status": "error"}


def load_defaults(*sections, path=DEFAULTS_CONF):
    defaults = {}
    if not os.path.exists(path):
        return defaults
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(path)
    for section in sections:
        if section in parser:
            defaults.update(parser[section].items())
    return defaults


def _parse_bool_default(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Invalid boolean default: {value!r}")


def _coerce_argparse_default(value, kwargs):
    action = kwargs.get("action")
    if action in (argparse.BooleanOptionalAction, "store_true", "store_false"):
        return _parse_bool_default(value)

    value_type = kwargs.get("type")
    if value_type is not None and value is not None:
        return value_type(value)

    return value


def parse_args_with_defaults(arg_defs, default_sections):
    defaults = load_defaults(*default_sections)
    parser = argparse.ArgumentParser()
    for args, kwargs in arg_defs:
        default_key = kwargs.get("dest", args[0].lstrip("-").replace("-", "_"))
        if default_key in defaults:
            kwargs["default"] = _coerce_argparse_default(defaults[default_key], kwargs)
        parser.add_argument(*args, **kwargs)
    return parser.parse_args()


def read_kv_config(path):
    config = {}
    if not os.path.exists(path):
        return config
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            config[key.strip()] = val.strip()
    return config


def read_network_conf():
    if not os.path.exists(NETWORK_CONF):
        print(f"❌ {NETWORK_CONF} not found", file=sys.stderr)
        return {}
    return read_kv_config(NETWORK_CONF)


def read_client_version_text(client_root=CLIENT_ROOT):
    version_path = os.path.join(client_root, "VERSION")
    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as handle:
            value = handle.read().strip()
        if value:
            return value

    install_state_path = os.path.join(client_root, "etc", "install-state.env")
    install_state = read_kv_config(install_state_path)
    value = install_state.get("INSTALLED_VERSION", "").strip()
    if value:
        return value

    raise SystemExit(
        f"[ERROR] Could not determine client version from {version_path} or {install_state_path}."
    )


def read_api_password():
    value = read_file(API_PASSWORD_FILE)
    if value is None:
        print("❌ Client API password file missing", file=sys.stderr)
        return None
    value = value.strip()
    if not value:
        print("❌ Client API password file is empty", file=sys.stderr)
        return None
    return value


def write_api_password(api_password: str):
    require_nonempty(api_password, "client API password")
    write_file(API_PASSWORD_FILE, api_password + "\n", mode=0o640, user="root")


def require_peer_uuid(config: dict) -> str:
    return require_nonempty(config.get("PEER_UUID"), "PEER_UUID")


def validate_api_password(config_server, port, api_password, network_name=None):
    requests = require_requests()
    probe_network_name = network_name or "__sensos_auth_probe__"
    url = network_info_url(config_server, port, probe_network_name)
    headers = build_basic_auth_header(api_password)
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code in (200, 404):
            return {
                "ok": True,
                "reason": "accepted",
                "status_code": response.status_code,
                "url": url,
            }
        if response.status_code in (401, 403):
            return {
                "ok": False,
                "reason": "invalid_credentials",
                "status_code": response.status_code,
                "url": url,
            }
        return {
            "ok": False,
            "reason": "unexpected_response",
            "status_code": response.status_code,
            "url": url,
        }
    except requests.exceptions.ConnectionError:
        return {
            "ok": False,
            "reason": "unreachable",
            "status_code": None,
            "url": url,
        }
    except Exception as e:
        print(f"❌ Error testing client API password against {url}: {e}", file=sys.stderr)
        return {
            "ok": False,
            "reason": "error",
            "status_code": None,
            "url": url,
        }


def fetch_network_info(config_server, port, api_password, network_name, timeout=5):
    requests = require_requests()
    headers = build_basic_auth_header(api_password)
    url = network_info_url(config_server, port, network_name)
    return requests.get(url, headers=headers, timeout=timeout)


def get_api_password(config_server, port, network_name=None):
    health = get_server_health(config_server, port)
    if not health["reachable"]:
        print(
            f"❌ Cannot reach configuration server at {config_server}:{port}.",
            file=sys.stderr,
        )
        print("📡 Is the device online? Is the server address correct?")
        return None
    if not health["ready"]:
        print(
            f"❌ Configuration server at {config_server}:{port} is not ready yet ({health['status']}).",
            file=sys.stderr,
        )
        return None
    tries = 3
    for attempt in range(tries):
        if os.path.exists(API_PASSWORD_FILE):
            stored_password = read_file(API_PASSWORD_FILE)
            print("Testing stored client API password...")
            validation = validate_api_password(
                config_server,
                port,
                stored_password,
                network_name=network_name,
            )
            if validation["ok"]:
                print("✅ Client API password from file is valid.")
                return stored_password
            elif validation["reason"] == "invalid_credentials":
                print("⚠️ Stored client API password is invalid.", file=sys.stderr)
            elif validation["reason"] == "unreachable":
                print(
                    f"❌ Lost contact with configuration server at {config_server}:{port}.",
                    file=sys.stderr,
                )
                return None
            else:
                print(
                    "⚠️ Could not validate stored client API password due to an unexpected "
                    f"server response ({validation['reason']}).",
                    file=sys.stderr,
                )
        api_password = input("🔑 Enter client API password: ").strip()
        validation = validate_api_password(
            config_server,
            port,
            api_password,
            network_name=network_name,
        )
        if validation["ok"]:
            if not api_password:
                print("❌ Error: client API password is empty. Not saving.", file=sys.stderr)
                continue
            write_file(API_PASSWORD_FILE, api_password + "\n", mode=0o640, user="root")
            print(f"✅ Client API password saved securely in {API_PASSWORD_FILE}.")
            return api_password
        if validation["reason"] == "invalid_credentials":
            print("❌ Client API password is invalid, please try again.", file=sys.stderr)
            continue
        if validation["reason"] == "unreachable":
            print(
                f"❌ Cannot reach configuration server at {config_server}:{port}.",
                file=sys.stderr,
            )
            print("📡 Is the device online? Is the server address correct?")
            return None
        print(
            "❌ Unable to validate client API password due to an unexpected "
            f"server response ({validation['reason']}).",
            file=sys.stderr,
        )
    print(
        "🚫 Failed to provide a valid client API password after 3 attempts.", file=sys.stderr
    )
    return None


def compute_api_server_wg_ip(client_wg_ip):
    parts = client_wg_ip.split(".")
    if len(parts) != 4:
        print(
            f"❌ Error: Invalid client WireGuard IP format: {client_wg_ip}",
            file=sys.stderr,
        )
        return None
    return f"{parts[0]}.{parts[1]}.0.1"


def _truthy_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _running_under_systemd() -> bool:
    return any(os.environ.get(name) for name in ("INVOCATION_ID", "JOURNAL_STREAM"))


class Tee:
    def __init__(self, log_file, mode="a", max_bytes=5 * 1024 * 1024, backup_count=5):
        self.terminal = sys.stdout
        self.log_file = log_file
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._line_start = True
        self._rotate_if_needed()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(log_file, flags, 0o664)
        try:
            os.chmod(log_file, 0o664)
        except OSError:
            pass
        self.log = os.fdopen(fd, mode)

    def _timestamp_prefix(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime())

    def _rotate_if_needed(self):
        try:
            size = os.path.getsize(self.log_file)
        except OSError:
            return
        if size < self.max_bytes:
            return

        oldest = f"{self.log_file}.{self.backup_count}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(self.backup_count - 1, 0, -1):
            source = f"{self.log_file}.{index}"
            target = f"{self.log_file}.{index + 1}"
            if os.path.exists(source):
                os.replace(source, target)
        os.replace(self.log_file, f"{self.log_file}.1")

    def _write_log(self, message):
        if not message:
            return
        for chunk in message.splitlines(keepends=True):
            if self._line_start and chunk not in ("\n", "\r\n"):
                self.log.write(self._timestamp_prefix())
            self.log.write(chunk)
            self._line_start = chunk.endswith("\n")
        if message and not message.endswith("\n"):
            self._line_start = False

    def write(self, message):
        self.terminal.write(message)
        self.terminal.flush()
        self._write_log(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def setup_logging(log_filename=None):
    script_name = os.path.basename(sys.argv[0])
    if "." in script_name:
        script_name = script_name.split(".")[0]
    if log_filename is None:
        log_filename = f"{script_name}.log"
    if _running_under_systemd() and not _truthy_env("SENSOS_FORCE_FILE_LOGGING", False):
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)
    sys.stdout = Tee(log_path)
    sys.stderr = sys.stdout
