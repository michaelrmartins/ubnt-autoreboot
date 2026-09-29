#!/usr/bin/env python3
"""
ubnt-autoreboot - restarts all UniFi access points through the controller API.

- Uses only the Python standard library (no pip / no venv).
- Settings are read from server.conf (same folder as the script, or via --config).
- Designed to be called by cron.

Logs (folder set in server.conf, default /var/log/ubnt-autoreboot):
  run.log   -> one line per run: timestamp, devices found, status
  error.log -> details of any failure (API error, controller down, AP offline)

Exit codes: 0 = all ok | 1 = partial or total failure | 2 = configuration error
"""

import argparse
import configparser
import http.cookiejar
import json
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONF = SCRIPT_DIR / "server.conf"
DEFAULT_LOG_DIR = "/var/log/ubnt-autoreboot"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class Logger:
    def __init__(self, log_dir):
        self.log_dir = Path(log_dir)

    def _write(self, filename, line):
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / filename, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            # No permission or disk full: at least show it on stderr
            print(f"[ubnt-autoreboot] could not write to {filename}: {e}", file=sys.stderr)
            print(line, file=sys.stderr)

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def run(self, found, restarted, status):
        found_txt = "-" if found is None else str(found)
        self._write(
            "run.log",
            f"{self._now()} | devices: {found_txt} | restarted: {restarted} | status: {status}",
        )

    def error(self, message):
        self._write("error.log", f"{self._now()} | {message}")


# --------------------------------------------------------------------------- #
# API client (internal/legacy API of the self-hosted controller)
# --------------------------------------------------------------------------- #
class UnifiError(Exception):
    """The API returned an error or the controller is unreachable."""


def _api_msg(body):
    try:
        return json.loads(body).get("meta", {}).get("msg")
    except (ValueError, AttributeError):
        return None


class UnifiClient:
    def __init__(self, host, port, site, verify_ssl, timeout):
        self.base = f"https://{host}:{port}"
        self.site = site
        self.timeout = timeout

        ctx = ssl.create_default_context()
        if not verify_ssl:  # local controllers usually use a self-signed certificate
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            msg = _api_msg(body) or body.strip()[:200]
            raise UnifiError(f"{method} {path}: HTTP {e.code} - {msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise UnifiError(f"controller unreachable at {self.base}: {reason}") from None

        try:
            result = json.loads(body)
        except ValueError:
            raise UnifiError(f"{method} {path}: response is not JSON: {body.strip()[:200]}") from None

        meta = result.get("meta", {})
        if meta.get("rc") != "ok":
            raise UnifiError(f"{method} {path}: API returned an error: {meta.get('msg', meta)}")
        return result.get("data", [])

    def login(self, username, password):
        self._request("POST", "/api/login", {"username": username, "password": password})

    def logout(self):
        try:
            self._request("POST", "/api/logout", {})
        except UnifiError:
            pass  # not critical

    def list_devices(self):
        return self._request("GET", f"/api/s/{self.site}/stat/device")

    def restart(self, mac):
        self._request("POST", f"/api/s/{self.site}/cmd/devmgr", {"cmd": "restart", "mac": mac})


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"configuration file not found: {path}")

    # interpolation=None: allows '%' in the password without breaking the parser
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")

    if not cp.has_section("controller"):
        raise ValueError(f"missing [controller] section in {path}")
    c = cp["controller"]

    for key in ("host", "username", "password"):
        if not c.get(key, "").strip():
            raise ValueError(f"'{key}' is not set in [controller] of {path}")

    types = cp.get("reboot", "device_types", fallback="uap")
    return {
        "host": c["host"].strip(),
        "port": c.getint("port", fallback=8443),
        "site": c.get("site", "default").strip(),
        "username": c["username"].strip(),
        "password": c["password"],
        "verify_ssl": c.getboolean("verify_ssl", fallback=False),
        "timeout": c.getint("timeout", fallback=15),
        "device_types": {t.strip() for t in types.split(",") if t.strip()},
        "delay": cp.getfloat("reboot", "delay_between", fallback=2),
        "log_dir": cp.get("logging", "log_dir", fallback=DEFAULT_LOG_DIR).strip(),
    }


# --------------------------------------------------------------------------- #
# Main routine
# --------------------------------------------------------------------------- #
def run(cfg, log, dry_run=False):
    client = UnifiClient(cfg["host"], cfg["port"], cfg["site"], cfg["verify_ssl"], cfg["timeout"])

    try:
        client.login(cfg["username"], cfg["password"])
        devices = client.list_devices()
    except UnifiError as e:
        log.error(f"ERROR | {e}")
        log.run(None, 0, "ERROR (controller/API)")
        return 1

    aps = [
        d for d in devices
        if d.get("type") in cfg["device_types"] and d.get("adopted", True)
    ]

    restarted, failures = 0, 0
    for i, ap in enumerate(aps):
        name = ap.get("name") or ap.get("mac")
        mac = ap.get("mac")

        if ap.get("state") != 1:  # 1 = connected
            if dry_run:
                print(f"[dry-run] offline (would be skipped): {name} ({mac})")
                continue
            log.error(f"WARNING | {name} ({mac}) is offline, not restarted")
            failures += 1
            continue

        if dry_run:
            print(f"[dry-run] would restart: {name} ({mac}) ip={ap.get('ip')}")
            continue

        try:
            client.restart(mac)
            restarted += 1
        except UnifiError as e:
            log.error(f"ERROR | failed to restart {name} ({mac}): {e}")
            failures += 1

        if cfg["delay"] > 0 and i < len(aps) - 1:
            time.sleep(cfg["delay"])

    client.logout()

    if dry_run:
        print(f"[dry-run] {len(aps)} device(s) found; nothing was restarted.")
        return 0

    if not aps:
        status = "OK (no APs found)"
    elif failures == 0:
        status = "OK"
    elif restarted == 0:
        status = "ERROR"
    else:
        status = "PARTIAL"

    log.run(len(aps), restarted, status)
    return 0 if failures == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="Restarts UniFi access points through the controller API.")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONF),
                        help=f"path to server.conf (default: {DEFAULT_CONF})")
    parser.add_argument("--dry-run", action="store_true",
                        help="only list the APs that would be restarted, without restarting or writing run.log")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except (ValueError, configparser.Error) as e:
        Logger(DEFAULT_LOG_DIR).error(f"ERROR | invalid configuration: {e}")
        print(f"[ubnt-autoreboot] invalid configuration: {e}", file=sys.stderr)
        return 2

    log = Logger(cfg["log_dir"])
    try:
        return run(cfg, log, dry_run=args.dry_run)
    except Exception:  # anything unexpected goes to error.log
        log.error("ERROR | unexpected failure:\n" + traceback.format_exc().rstrip())
        log.run(None, 0, "ERROR (unexpected)")
        return 1


if __name__ == "__main__":
    sys.exit(main())