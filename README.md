# ubnt-autoreboot

Restarts all UniFi access points through a self-hosted UniFi Controller's API. Built to be run unattended from cron.

- **No dependencies** — uses only the Python 3 standard library (no `pip`, no virtualenv).
- **Config file driven** — all settings live in `server.conf`.
- **Logs** each run and any failures to dedicated log files.

## Requirements

- Python 3.6+
- Network access to a self-hosted UniFi Controller (uses its internal/legacy API on port `8443` by default).
- A controller account with permission to restart devices.

## Setup

1. Copy the example config and edit it with your controller details:

   ```bash
   cp "server copy.example" server.conf
   ```

2. Protect the file — it contains a password:

   ```bash
   chmod 600 server.conf
   ```

3. Edit `server.conf`:

   ```ini
   [controller]
   host = 192.168.50.4
   port = 8443
   site = default
   username = admin-api
   password = your-password
   # Local controllers use a self-signed certificate; keep this false
   verify_ssl = false
   # Maximum time to wait for a controller response, in seconds
   timeout = 15

   [reboot]
   # Device types to restart (uap = access points). E.g.: uap,usw
   device_types = uap
   # Pause between restarts, in seconds
   delay_between = 2

   [logging]
   log_dir = /var/log/ubnt-autoreboot
   ```

   > **Tip:** create a dedicated read/write API account on the controller rather than reusing your admin login.

## Usage

Run against the default `server.conf` (in the same folder as the script):

```bash
./ubnt-autoreboot.py
```

Or point at a specific config:

```bash
./ubnt-autoreboot.py --config /etc/ubnt-autoreboot/server.conf
```

Preview what would happen without restarting anything (also skips writing `run.log`):

```bash
./ubnt-autoreboot.py --dry-run
```

### Options

| Flag | Description |
| --- | --- |
| `-c`, `--config PATH` | Path to `server.conf` (default: alongside the script). |
| `--dry-run` | List the APs that would be restarted, without restarting or logging. |

## How it works

1. Logs in to the controller API and lists all devices for the configured site.
2. Filters to the configured `device_types` that are adopted.
3. Restarts each connected AP, pausing `delay_between` seconds between devices.
4. Offline devices are skipped and recorded as warnings.
5. Logs out and writes a summary line to `run.log`.

## Logging

Files are written to `log_dir` (default `/var/log/ubnt-autoreboot`):

- **`run.log`** — one line per run: timestamp, devices found, number restarted, status.
- **`error.log`** — details of any failure (API error, controller unreachable, AP offline).

If the log directory isn't writable, messages fall back to stderr.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All OK |
| `1` | Partial or total failure |
| `2` | Configuration error |

## Running from cron

Restart the APs every day at 4:00 AM:

```cron
0 4 * * * /path/to/ubnt-autoreboot/ubnt-autoreboot.py --config /path/to/server.conf
```

Check results afterward with `tail /var/log/ubnt-autoreboot/run.log`.

## Security notes

- `server.conf` holds your controller password — keep it `chmod 600` and out of version control (`.gitignore` already excludes it).
- `verify_ssl` defaults to `false` because self-hosted controllers typically use a self-signed certificate. Set it to `true` if your controller has a valid certificate.
