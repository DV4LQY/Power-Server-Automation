import os
import paramiko
import time
import json
import csv
import smtplib
import requests
import re
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from concurrent.futures import ThreadPoolExecutor
import threading
from dotenv import load_dotenv

# SLA GRAPH (HEADLESS SAFE)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# PDF
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet

# -------- LOAD ENV --------
load_dotenv()

USE_VAULT = os.getenv("USE_VAULT", "false").lower() == "true"
USE_KEYRING = os.getenv("USE_KEYRING", "false").lower() == "true"

# -------- SECRET MANAGEMENT --------
def get_secret(key, default=None):
    if USE_VAULT:
        try:
            import hvac
            client = hvac.Client(
                url=os.getenv("VAULT_URL"),
                token=os.getenv("VAULT_TOKEN")
            )
            path = os.getenv("VAULT_SECRET_PATH")
            secret = client.secrets.kv.v2.read_secret_version(path=path)
            return secret["data"]["data"].get(key, default)
        except Exception as e:
            print(f"Vault error: {e}")

    if USE_KEYRING:
        try:
            import keyring
            val = keyring.get_password("server_monitor", key)
            if val:
                return val
        except Exception as e:
            print(f"Keyring error: {e}")

    return os.getenv(key, default)

# -------- CONFIG --------
SERVERS = [
    {
        "name": "ENGAS_SERVER-1-IDRAC",
        "host": get_secret("SSH1_HOST"),
        "port": int(get_secret("SSH1_PORT")),
        "type": "idrac",
        "username": get_secret("SSH1_USER"),
        "password": get_secret("SSH1_PASS"),
        "enabled": True
    },
    {
        "name": "NMS_SERVER-2-IDRAC",
        "host": get_secret("SSH2_HOST"),
        "port": int(get_secret("SSH2_PORT")),
        "type": "idrac",
        "username": get_secret("SSH2_USER"),
        "password": get_secret("SSH2_PASS"),
        "enabled": False
    },
    {
        "name": "ADDS_SERVER-3-BMC",
        "host": get_secret("SSH3_HOST"),
        "port": int(get_secret("SSH3_PORT")),
        "type": "bmc",
        "username": get_secret("SSH3_USER"),
        "password": get_secret("SSH3_PASS"),
        "enabled": False
    },
    {
        "name": "DEV_SERVER-4-BMC",
        "host": get_secret("SSH4_HOST"),
        "port": int(get_secret("SSH4_PORT")),
        "type": "bmc",
        "username": get_secret("SSH4_USER"),
        "password": get_secret("SSH4_PASS"),
        "enabled": True
    },
    {
        "name": "SERVER-5-BMC",
        "host": get_secret("SSH5_HOST"),
        "port": int(get_secret("SSH5_PORT")),
        "type": "bmc",
        "username": get_secret("SSH5_USER"),
        "password": get_secret("SSH5_PASS"),
        "enabled": False
    }
]

LOG_FILE = "/home/lqy/power_server.log"
STATE_FILE = "/home/lqy/server_state.json"
METRICS_FILE = "/home/lqy/server_metrics.json"

SMTP_HOST = get_secret("SMTP_HOST")
SMTP_PORT = int(get_secret("SMTP_PORT"))
SMTP_USER = get_secret("SMTP_USER")
SMTP_PASS = get_secret("SMTP_PASS")
EMAIL_TO = get_secret("EMAIL_TO")

TELEGRAM_BOT_TOKEN = get_secret("TG_TOKEN")
TELEGRAM_CHAT_ID = get_secret("TG_CHAT_ID")

MAX_LOG_SIZE = 1 * 1024 * 1024
MAX_LOG_BACKUPS = 5

COMMAND_DELAY = 10
VERIFY_DELAY = 8
MAX_RETRIES = 3
COOLDOWN_MINUTES = 30
TELEGRAM_POLL_INTERVAL = 20
TELEGRAM_CONNECT_TIMEOUT = 5
TELEGRAM_READ_TIMEOUT = 15
TELEGRAM_RETRY_BASE_SECONDS = 10
TELEGRAM_RETRY_MAX_SECONDS = 120
TELEGRAM_ERROR_LOG_INTERVAL = 300
SSH_CONNECT_TIMEOUT = 10
SSH_AUTH_TIMEOUT = 10
SSH_BANNER_TIMEOUT = 10

# Monday-Friday schedule (server local time)
WEEKDAY_POWER_ON_TIME = dt_time(6, 30)
WEEKDAY_POWER_ON_END_TIME = dt_time(7, 0)
WEEKDAY_RECOVERY_START_TIME = dt_time(7, 0)
WEEKDAY_RECOVERY_END_TIME = dt_time(16, 0)
WEEKDAY_FORCE_MONITOR_START_TIME = dt_time(20, 0)
WEEKDAY_FORCE_OFF_TIME = dt_time(22, 15)
WEEKDAY_FINAL_SUMMARY_TIME = dt_time(22, 30)

# Saturday-Sunday schedule (server local time)
WEEKEND_POWER_ON_TIME = dt_time(7, 30)
WEEKEND_POWER_ON_END_TIME = dt_time(8, 0)
WEEKEND_RECOVERY_START_TIME = dt_time(8, 0)
WEEKEND_RECOVERY_END_TIME = dt_time(16, 0)
WEEKEND_FORCE_MONITOR_START_TIME = dt_time(20, 0)
WEEKEND_FORCE_OFF_TIME = dt_time(20, 15)
WEEKEND_FORCE_OFF_END_TIME = dt_time(20, 45)
WEEKEND_FINAL_SUMMARY_TIME = dt_time(21, 30)

log_lock = threading.Lock()
ACTIVE_SERVERS = [s for s in SERVERS if s.get("enabled", True)]
telegram_poll_health = {
    "consecutive_failures": 0,
    "next_retry_at": 0.0,
    "last_error_log_at": 0.0,
    "last_error_type": None
}
telegram_missing_config_logged = False

def log_active_servers():
    enabled = [f"{s['name']} ({s['host']}) [{s['type'].upper()}]" for s in ACTIVE_SERVERS]
    if enabled:
        log("ACTIVE SERVERS: " + "; ".join(enabled))
    else:
        log("ACTIVE SERVERS: none")

# -------- LOG --------
def rotate_log_if_needed():
    log_path = Path(LOG_FILE)
    if not log_path.exists() or log_path.stat().st_size < MAX_LOG_SIZE:
        return

    for i in range(MAX_LOG_BACKUPS, 0, -1):
        older = Path(f"{LOG_FILE}.{i}")
        newer = Path(f"{LOG_FILE}.{i+1}")
        if older.exists():
            if i == MAX_LOG_BACKUPS:
                older.unlink()
            else:
                older.rename(newer)

    log_path.rename(f"{LOG_FILE}.1")

def log(msg):
    with log_lock:
        rotate_log_if_needed()
        line = f"{datetime.now()} - {msg}"
        print(line)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")

def log_disabled_servers():
    for s in SERVERS:
        if not s.get("enabled", True):
            log(f"[DISABLED] Monitoring skipped for {s['name']} ({s['host']})")

# -------- JSON --------
def load_json(path):
    file_path = Path(path)
    if not file_path.exists():
        return {}

    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"JSON LOAD ERROR {path}: {e}")
        return {}

def save_json(path, data):
    file_path = Path(path)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, file_path)

def ran_today(state, key, now=None):
    now = now or datetime.now()
    return state.get(key) == now.date().isoformat()

def mark_ran_today(state, key, now=None):
    now = now or datetime.now()
    state[key] = now.date().isoformat()

# -------- SCHEDULE --------
def is_weekday(now=None):
    now = now or datetime.now()
    return now.weekday() < 5

def is_weekend(now=None):
    now = now or datetime.now()
    return now.weekday() >= 5

def format_schedule_time(value):
    return datetime.combine(datetime.today(), value).strftime("%I:%M %p").lstrip("0")

def is_power_on_window(now=None):
    now = now or datetime.now()
    if is_weekday(now):
        return WEEKDAY_POWER_ON_TIME <= now.time() < WEEKDAY_POWER_ON_END_TIME
    return WEEKEND_POWER_ON_TIME <= now.time() < WEEKEND_POWER_ON_END_TIME

def is_recovery_window(now=None):
    now = now or datetime.now()
    if is_weekday(now):
        return WEEKDAY_RECOVERY_START_TIME <= now.time() < WEEKDAY_RECOVERY_END_TIME
    return WEEKEND_RECOVERY_START_TIME <= now.time() < WEEKEND_RECOVERY_END_TIME

def is_force_monitor_window(now=None):
    now = now or datetime.now()
    if is_weekday(now):
        return WEEKDAY_FORCE_MONITOR_START_TIME <= now.time() < WEEKDAY_FORCE_OFF_TIME
    return WEEKEND_FORCE_MONITOR_START_TIME <= now.time() < WEEKEND_FORCE_OFF_TIME

def is_force_off_window(now=None):
    now = now or datetime.now()
    if is_weekday(now):
        return WEEKDAY_FORCE_OFF_TIME <= now.time() < WEEKDAY_FINAL_SUMMARY_TIME
    return WEEKEND_FORCE_OFF_TIME <= now.time() < WEEKEND_FORCE_OFF_END_TIME

def schedule_slot_key(now=None, minutes=30):
    now = now or datetime.now()
    slot_minute = (now.minute // minutes) * minutes
    return now.replace(minute=slot_minute, second=0, microsecond=0).isoformat()

def should_send_sla(now=None):
    now = now or datetime.now()
    return now.day == 1 and now.time() >= dt_time(9, 0)

def build_day_schedule(day):
    points = []
    wd = day.weekday()

    if wd < 5:  # Monday-Friday
        power_on_time = WEEKDAY_POWER_ON_TIME
        recovery_start = WEEKDAY_RECOVERY_START_TIME
        recovery_end = WEEKDAY_RECOVERY_END_TIME
        force_monitor_start = WEEKDAY_FORCE_MONITOR_START_TIME
        force_off_time = WEEKDAY_FORCE_OFF_TIME
        fixed_points = [WEEKDAY_FORCE_OFF_TIME, WEEKDAY_FINAL_SUMMARY_TIME]
    else:  # Saturday-Sunday
        power_on_time = WEEKEND_POWER_ON_TIME
        recovery_start = WEEKEND_RECOVERY_START_TIME
        recovery_end = WEEKEND_RECOVERY_END_TIME
        force_monitor_start = WEEKEND_FORCE_MONITOR_START_TIME
        force_off_time = WEEKEND_FORCE_OFF_TIME
        fixed_points = [WEEKEND_FORCE_OFF_TIME, WEEKEND_FINAL_SUMMARY_TIME]

    points.append(datetime.combine(day, power_on_time))

    # Recovery checkpoints every 30 minutes up to, but not including, 4:00 PM.
    t = datetime.combine(day, recovery_start)
    end = datetime.combine(day, recovery_end)
    while t < end:
        points.append(t)
        t += timedelta(minutes=30)

    # State refresh checkpoints before the scheduled force shutdown.
    t = datetime.combine(day, force_monitor_start)
    end = datetime.combine(day, force_off_time)
    while t < end:
        points.append(t)
        t += timedelta(minutes=30)

    for checkpoint in fixed_points:
        points.append(datetime.combine(day, checkpoint))

    # Monthly SLA job
    if day.day == 1:
        points.append(datetime.combine(day, dt_time(9, 0)))

    return sorted(set(points))

def next_wakeup_time(now=None):
    now = now or datetime.now()

    for offset in range(0, 8):
        day = now.date() + timedelta(days=offset)
        for candidate in build_day_schedule(day):
            # The main loop processes the current window before sleeping. Selecting
            # only a future checkpoint prevents the same slot from running again.
            if candidate > now:
                return candidate

    return now + timedelta(minutes=30)

def sleep_until_next_wakeup():
    now = datetime.now()
    target = next_wakeup_time(now)
    log(f"SLEEPING UNTIL {target.strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        process_telegram_commands()

        now = datetime.now()
        secs = (target - now).total_seconds()

        if secs <= 0:
            break

        time.sleep(min(TELEGRAM_POLL_INTERVAL, max(0.1, secs)))

# -------- EMAIL --------
def send_email(subject, body, attachments=None):
    try:
        recipients = [e.strip() for e in EMAIL_TO.split(",")]

        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if attachments:
            for file in attachments:
                with open(file, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f"attachment; filename={Path(file).name}"
                    )
                    msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())

        log("SLA EMAIL SENT")
        return True
    except Exception as e:
        log(f"EMAIL ERROR {e}")
        return False

# -------- SSH --------
def wait_prompt(shell):
    buf, start = "", time.time()
    while True:
        if shell.recv_ready():
            buf += shell.recv(4096).decode(errors="ignore")
            if "->" in buf or "#" in buf:
                return buf
        if time.time() - start > 10:
            return buf
        time.sleep(0.2)

def open_shell(server):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        server["host"],
        port=server["port"],
        username=server["username"],
        password=server["password"],
        timeout=SSH_CONNECT_TIMEOUT,
        auth_timeout=SSH_AUTH_TIMEOUT,
        banner_timeout=SSH_BANNER_TIMEOUT
    )
    sh = c.invoke_shell()
    wait_prompt(sh)
    return c, sh

def run_cmd(sh, server, cmd):
    log(f"[{server['host']}] CMD: {cmd}")
    sh.send(cmd + "\n")
    out = wait_prompt(sh)
    log(f"[{server['host']}] OUT:\n{out}")
    time.sleep(COMMAND_DELAY)
    return out

# -------- POWER --------
def get_power(sh, server):
    if server["type"] == "idrac":
        out = run_cmd(sh, server, "racadm serveraction powerstatus")

        m = re.search(
            r"Server\s+power\s+status\s*:\s*(ON|OFF)",
            out,
            re.IGNORECASE
        )

        if m:
            return m.group(1).upper()

        log(f"[{server['host']}] WARNING: unable to parse iDRAC power status")
        log(f"[{server['host']}] RAW POWER STATUS OUT:\n{out}")
        return "UNKNOWN"

    out = run_cmd(sh, server, "show /system1/pwrmgtsvc1")
    m = re.search(r"PowerState\s*[:=]\s*(\d+)", out, re.IGNORECASE)

    if m:
        return "ON" if m.group(1) == "1" else "OFF"

    log(f"[{server['host']}] WARNING: unable to parse BMC power status")
    log(f"[{server['host']}] RAW POWER STATUS OUT:\n{out}")
    return "UNKNOWN"

def power_on(sh, server):
    if server["type"] == "idrac":
        run_cmd(sh, server, "racadm serveraction powerup")
        return

    out = run_cmd(sh, server, "start /system1/pwrmgtsvc1")
    if re.search(r"(invalid|error|not supported|syntax)", out, re.IGNORECASE):
        log(f"[{server['host']}] BMC direct start command failed, trying path-based start fallback")
        run_cmd(sh, server, "cd /system1/pwrmgtsvc1")
        run_cmd(sh, server, "start")

def power_off(sh, server):
    if server["type"] == "idrac":
        run_cmd(sh, server, "racadm serveraction powerdown")
    else:
        run_cmd(sh, server, "cd /system1/pwrmgtsvc1; stop")

# -------- TELEGRAM --------
def compact_telegram_error(error):
    """Return a safe error label without logging the bot-token URL."""
    return type(error).__name__

def telegram_api_description(data):
    if not isinstance(data, dict):
        return "invalid Telegram API response"
    description = str(data.get("description") or "Telegram API request failed")
    if TELEGRAM_BOT_TOKEN:
        description = description.replace(str(TELEGRAM_BOT_TOKEN), "<redacted>")
    return description[:240]

def safe_telegram_log_text(value):
    text = str(value)
    if TELEGRAM_BOT_TOKEN:
        text = text.replace(str(TELEGRAM_BOT_TOKEN), "<redacted>")
    return text[:240]

def telegram_poll_backoff_active():
    return time.monotonic() < telegram_poll_health["next_retry_at"]

def register_telegram_poll_failure(error_type, detail=None, retry_after=None):
    now = time.monotonic()
    failures = telegram_poll_health["consecutive_failures"] + 1
    calculated_delay = min(
        TELEGRAM_RETRY_BASE_SECONDS * (2 ** min(failures - 1, 6)),
        TELEGRAM_RETRY_MAX_SECONDS
    )
    delay = max(calculated_delay, int(retry_after or 0))

    telegram_poll_health["consecutive_failures"] = failures
    telegram_poll_health["next_retry_at"] = now + delay
    telegram_poll_health["last_error_type"] = error_type

    last_log_at = telegram_poll_health["last_error_log_at"]
    if failures == 1 or now - last_log_at >= TELEGRAM_ERROR_LOG_INTERVAL:
        message = (
            f"TG GETUPDATES UNAVAILABLE type={error_type} "
            f"failures={failures} retry_in={delay}s"
        )
        if detail:
            message += f" detail={str(detail)[:240]}"
        log(message)
        telegram_poll_health["last_error_log_at"] = now

def register_telegram_poll_success():
    failures = telegram_poll_health["consecutive_failures"]
    if failures:
        log(f"TG GETUPDATES RECOVERED after {failures} failed attempt(s)")

    telegram_poll_health["consecutive_failures"] = 0
    telegram_poll_health["next_retry_at"] = 0.0
    telegram_poll_health["last_error_log_at"] = 0.0
    telegram_poll_health["last_error_type"] = None

def send_telegram(msg, reply_markup=None, chat_id=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("TG ERROR missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False

    try:
        payload = {
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": msg
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=(TELEGRAM_CONNECT_TIMEOUT, TELEGRAM_READ_TIMEOUT)
        )

        if not response.ok:
            log(
                f"TG SEND ERROR status={response.status_code} "
                f"body={safe_telegram_log_text(response.text)}"
            )
            return False

        data = response.json()
        if not data.get("ok"):
            log(f"TG SEND ERROR response={data}")
            return False

        return True

    except Exception as e:
        log(f"TG SEND ERROR type={compact_telegram_error(e)}")
        return False

def answer_telegram_callback(callback_query_id, text=None, show_alert=False):
    if not TELEGRAM_BOT_TOKEN:
        log("TG CALLBACK ERROR missing TELEGRAM_BOT_TOKEN")
        return False

    try:
        payload = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert
        }
        if text:
            payload["text"] = text[:200]

        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json=payload,
            timeout=(TELEGRAM_CONNECT_TIMEOUT, TELEGRAM_READ_TIMEOUT)
        )
        data = response.json()
        if not response.ok or not data.get("ok"):
            log(f"TG CALLBACK ERROR status={response.status_code} response={data}")
            return False
        return True
    except Exception as e:
        log(f"TG CALLBACK ERROR type={compact_telegram_error(e)}")
        return False

def edit_telegram_message(chat_id, message_id, msg, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id or not message_id:
        return False

    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": msg
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
            json=payload,
            timeout=(TELEGRAM_CONNECT_TIMEOUT, TELEGRAM_READ_TIMEOUT)
        )
        data = response.json()
        if response.ok and data.get("ok"):
            return True

        description = telegram_api_description(data)
        if "message is not modified" in description.lower():
            return True

        log(
            f"TG EDIT ERROR status={response.status_code} "
            f"detail={safe_telegram_log_text(description)}"
        )
        return False
    except Exception as e:
        log(f"TG EDIT ERROR type={compact_telegram_error(e)}")
        return False

def send_telegram_document(file_path, caption=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("TG DOCUMENT ERROR missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False

    if not file_path or not os.path.exists(file_path):
        log(f"TG DOCUMENT ERROR file not found: {file_path}")
        return False

    try:
        data = {"chat_id": TELEGRAM_CHAT_ID}
        if caption:
            data["caption"] = caption

        with open(file_path, "rb") as f:
            files = {
                "document": (Path(file_path).name, f)
            }

            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data=data,
                files=files,
                timeout=60
            )

        if not response.ok:
            log(
                f"TG DOCUMENT SEND ERROR status={response.status_code} "
                f"body={safe_telegram_log_text(response.text)}"
            )
            return False

        result = response.json()
        if not result.get("ok"):
            log(f"TG DOCUMENT SEND ERROR response={result}")
            return False

        return True

    except Exception as e:
        log(f"TG DOCUMENT ERROR type={compact_telegram_error(e)}")
        return False


def send_telegram_photo(file_path, caption=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("TG PHOTO ERROR missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False

    if not file_path or not os.path.exists(file_path):
        log(f"TG PHOTO ERROR file not found: {file_path}")
        return False

    try:
        data = {"chat_id": TELEGRAM_CHAT_ID}
        if caption:
            data["caption"] = caption

        with open(file_path, "rb") as f:
            files = {
                "photo": (Path(file_path).name, f)
            }

            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data=data,
                files=files,
                timeout=60
            )

        if not response.ok:
            log(
                f"TG PHOTO SEND ERROR status={response.status_code} "
                f"body={safe_telegram_log_text(response.text)}"
            )
            return False

        result = response.json()
        if not result.get("ok"):
            log(f"TG PHOTO SEND ERROR response={result}")
            return False

        return True

    except Exception as e:
        log(f"TG PHOTO ERROR type={compact_telegram_error(e)}")
        return False

def get_telegram_updates(offset=None, timeout=0):
    global telegram_missing_config_logged

    if not TELEGRAM_BOT_TOKEN:
        if not telegram_missing_config_logged:
            log("TG GETUPDATES DISABLED: missing TELEGRAM_BOT_TOKEN")
            telegram_missing_config_logged = True
        return []

    if telegram_poll_backoff_active():
        return []

    try:
        params = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "edited_message", "callback_query"])
        }
        if offset is not None:
            params["offset"] = offset

        response = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params=params,
            timeout=(TELEGRAM_CONNECT_TIMEOUT, TELEGRAM_READ_TIMEOUT)
        )
        try:
            data = response.json()
        except ValueError:
            register_telegram_poll_failure(
                "InvalidJSON",
                detail=f"HTTP {response.status_code}"
            )
            return []

        if response.ok and data.get("ok"):
            register_telegram_poll_success()
            return data.get("result", [])

        parameters = data.get("parameters") if isinstance(data, dict) else {}
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        register_telegram_poll_failure(
            f"API_{response.status_code}",
            detail=telegram_api_description(data),
            retry_after=retry_after
        )
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
        register_telegram_poll_failure(compact_telegram_error(e))
    except requests.exceptions.ConnectionError as e:
        register_telegram_poll_failure(compact_telegram_error(e))
    except requests.exceptions.RequestException as e:
        register_telegram_poll_failure(compact_telegram_error(e))
    except Exception as e:
        register_telegram_poll_failure(compact_telegram_error(e))

    return []

def normalize_server_identifier(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())

def get_server_target_list():
    return "\n".join(
        f"- {server['name']} ({server['host']}) "
        f"[{'ACTIVE' if server in ACTIVE_SERVERS else 'DISABLED'}]"
        for server in SERVERS
    )

def telegram_response(text, reply_markup=None):
    return {
        "text": text,
        "reply_markup": reply_markup
    }

def build_main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Server Status", "callback_data": "menu:status"},
                {"text": "🗓 Schedule", "callback_data": "action:schedule"}
            ],
            [
                {"text": "🟢 Turn ON", "callback_data": "menu:poweron"},
                {"text": "🔴 Turn OFF", "callback_data": "menu:poweroff"}
            ],
            [
                {"text": "📊 Send SLA Graph", "callback_data": "action:graph"},
                {"text": "❓ Help", "callback_data": "action:help"}
            ]
        ]
    }

def build_server_menu(action):
    labels = {
        "status": "Check",
        "poweron": "Turn ON",
        "poweroff": "Turn OFF"
    }
    rows = []

    if action == "status":
        rows.append([{"text": "📋 All Active Servers", "callback_data": "action:summary"}])

    for server in ACTIVE_SERVERS:
        rows.append([{
            "text": f"{labels[action]}: {server['name']}",
            "callback_data": f"{action}:{get_server_callback_token(server)}"
        }])

    rows.append([{"text": "⬅️ Main Menu", "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}

def get_server_callback_token(server):
    identity = f"{server.get('name', '')}|{server.get('host', '')}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]

def get_active_server_by_callback_token(value):
    for server in ACTIVE_SERVERS:
        if get_server_callback_token(server) == value:
            return server
    return None

def build_schedule_summary():
    return (
        "🗓 AUTOMATION SCHEDULE\n"
        "Times use the server's local clock.\n\n"
        "Monday-Friday\n"
        f"• Power ON: {format_schedule_time(WEEKDAY_POWER_ON_TIME)}\n"
        f"• Recovery: every 30 minutes, "
        f"{format_schedule_time(WEEKDAY_RECOVERY_START_TIME)}-"
        f"{format_schedule_time(WEEKDAY_RECOVERY_END_TIME)}\n"
        f"• Force shutdown: {format_schedule_time(WEEKDAY_FORCE_OFF_TIME)}\n"
        f"• Final status: {format_schedule_time(WEEKDAY_FINAL_SUMMARY_TIME)}\n\n"
        "Saturday-Sunday\n"
        f"• Power ON: {format_schedule_time(WEEKEND_POWER_ON_TIME)}\n"
        f"• Recovery: every 30 minutes, "
        f"{format_schedule_time(WEEKEND_RECOVERY_START_TIME)}-"
        f"{format_schedule_time(WEEKEND_RECOVERY_END_TIME)}\n"
        f"• Force shutdown: {format_schedule_time(WEEKEND_FORCE_OFF_TIME)}\n"
        f"• Final status: {format_schedule_time(WEEKEND_FINAL_SUMMARY_TIME)}"
    )

def build_telegram_help():
    return (
        "🤖 SERVER CONTROL HELP\n"
        "Use the buttons below or send a command:\n\n"
        "/menu - open the control panel\n"
        "/status <server_name_or_host>\n"
        "/getstatus <server_name_or_host>\n"
        "/poweron <server_name_or_host>\n"
        "/poweroff <server_name_or_host>\n"
        "/summary - status of all active servers\n"
        "/schedule - show the automation schedule\n"
        "/sendgraph or /send graph\n\n"
        "Configured servers:\n"
        f"{get_server_target_list()}"
    )

def find_server(identifier):
    key = normalize_server_identifier(identifier)
    exact_matches = []
    partial_matches = []

    for server in SERVERS:
        values = [
            normalize_server_identifier(server["name"]),
            normalize_server_identifier(server["host"])
        ]

        if key in values:
            exact_matches.append(server)
        elif any(key in value or value in key for value in values):
            partial_matches.append(server)

    if len(exact_matches) == 1:
        return exact_matches[0], []

    if not exact_matches and len(partial_matches) == 1:
        return partial_matches[0], []

    return None, exact_matches or partial_matches

def get_live_server_status(server):
    c = None
    try:
        c, sh = open_shell(server)
        return get_power(sh, server)
    finally:
        if c:
            c.close()

def build_power_state_summary(title="📋 SERVER POWER STATE SUMMARY"):
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda s: refresh_server_state(s, st, mt), ACTIVE_SERVERS))
    except Exception as e:
        log(f"SUMMARY REFRESH ERROR {e}")
    finally:
        save_json(STATE_FILE, st)
        save_json(METRICS_FILE, mt)

    lines = [title]
    for server in ACTIVE_SERVERS:
        host = server["host"]
        name = server["name"]
        current = st.get(host, {}).get("last_state", "UNKNOWN")
        lines.append(f"{name} ({host}) : {current}")

    return "\n".join(lines)

def manual_power_summary():
    return build_power_state_summary("📋 SERVER POWER STATE SUMMARY")

def manual_power_on(server):
    host = server["host"]
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)
    c = None

    try:
        c, sh = open_shell(server)
        curr = get_power(sh, server)
        log(f"[{host}] MANUAL POWER ON pre-check state = {curr}")

        if curr == "ON":
            final_state = "ON"
            message = f"✅ POWER ON COMPLETED\n{server['name']} ({host}) is already ON."
        elif curr == "UNKNOWN":
            final_state = "UNKNOWN"
            message = f"❌ POWER ON FAILED\n{server['name']} ({host}) state could not be verified."
        else:
            confirm = get_power(sh, server)
            log(f"[{host}] MANUAL POWER ON confirm state = {confirm}")

            if confirm == "ON":
                final_state = "ON"
                message = f"✅ POWER ON COMPLETED\n{server['name']} ({host}) is already ON."
            elif confirm == "UNKNOWN":
                final_state = "UNKNOWN"
                message = f"❌ POWER ON FAILED\n{server['name']} ({host}) state could not be verified."
            else:
                success = False

                for _ in range(MAX_RETRIES):
                    power_on(sh, server)
                    time.sleep(VERIFY_DELAY)
                    after = get_power(sh, server)
                    log(f"[{host}] MANUAL POWER ON post-check state = {after}")

                    if after == "ON":
                        success = True
                        break

                if success:
                    final_state = "ON"
                    message = f"✅ POWER ON SUCCESS\n{server['name']} ({host}) is ON."
                else:
                    final_state = "FAILED"
                    message = f"❌ POWER ON FAILED\n{server['name']} ({host}) remained OFF."

        actual_state = (
            "ON" if final_state == "ON"
            else "OFF" if final_state in ("OFF", "FAILED")
            else "UNKNOWN"
        )
        st.setdefault(host, {})["last_state"] = actual_state
        st.setdefault(host, {})["last_observed_power"] = actual_state
        st.setdefault(host, {})["initialized"] = True
        update_metrics(mt, host, final_state)

        save_json(STATE_FILE, st)
        save_json(METRICS_FILE, mt)
        return message

    except Exception as e:
        log(f"[{host}] MANUAL POWER ON ERROR {e}")
        return f"❌ POWER ON FAILED\n{server['name']} ({host})\n{e}"
    finally:
        if c:
            c.close()

def manual_power_off(server):
    host = server["host"]
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)
    c = None

    try:
        c, sh = open_shell(server)
        curr = get_power(sh, server)
        log(f"[{host}] MANUAL POWER OFF pre-check state = {curr}")

        if curr == "UNKNOWN":
            final_state = "UNKNOWN"
            message = f"❌ POWER OFF FAILED\n{server['name']} ({host}) state could not be verified."
        elif curr != "ON":
            final_state = "OFF"
            message = f"✅ POWER OFF COMPLETED\n{server['name']} ({host}) is already OFF."
        else:
            confirm = get_power(sh, server)
            log(f"[{host}] MANUAL POWER OFF confirm state = {confirm}")

            if confirm == "UNKNOWN":
                final_state = "UNKNOWN"
                message = f"❌ POWER OFF FAILED\n{server['name']} ({host}) state could not be verified."
            elif confirm != "ON":
                final_state = "OFF"
                message = f"✅ POWER OFF COMPLETED\n{server['name']} ({host}) is already OFF."
            else:
                power_off(sh, server)
                time.sleep(VERIFY_DELAY)
                after = get_power(sh, server)
                log(f"[{host}] MANUAL POWER OFF post-check state = {after}")

                if after == "OFF":
                    final_state = "OFF"
                    message = f"✅ POWER OFF SUCCESS\n{server['name']} ({host}) is OFF."
                elif after == "ON":
                    final_state = "ON"
                    message = f"❌ POWER OFF FAILED\n{server['name']} ({host}) remained ON."
                else:
                    final_state = "UNKNOWN"
                    message = f"❌ POWER OFF FAILED\n{server['name']} ({host}) final state is unknown."

        st.setdefault(host, {})["last_state"] = final_state
        st.setdefault(host, {})["last_observed_power"] = final_state
        st.setdefault(host, {})["initialized"] = True
        update_metrics(mt, host, final_state)

        save_json(STATE_FILE, st)
        save_json(METRICS_FILE, mt)
        return message

    except Exception as e:
        log(f"[{host}] MANUAL POWER OFF ERROR {e}")
        return f"❌ POWER OFF FAILED\n{server['name']} ({host})\n{e}"
    finally:
        if c:
            c.close()

def handle_telegram_command(text):
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/start", "start", "/menu", "menu"):
        return telegram_response(
            "🖥 SERVER CONTROL PANEL\nSelect an action:",
            build_main_menu()
        )

    if command in ("/help", "help"):
        return telegram_response(build_telegram_help(), build_main_menu())

    if command in ("/schedule", "schedule"):
        return telegram_response(build_schedule_summary(), build_main_menu())

    if command in ("/status", "/getstatus"):
        if not argument:
            return telegram_response(
                "📋 Select an active server to check:",
                build_server_menu("status")
            )

        server, matches = find_server(argument)
        if not server:
            if matches:
                options = "\n".join(f"- {s['name']} ({s['host']})" for s in matches)
                text = f"❌ STATUS COMMAND FAILED\nMultiple matches for '{argument}':\n{options}"
            else:
                text = f"❌ STATUS COMMAND FAILED\nServer not found: {argument}"
            return telegram_response(text, build_server_menu("status"))

        status = get_live_server_status(server)
        return telegram_response(
            f"✅ STATUS CHECK COMPLETED\n{server['name']} ({server['host']}) : {status}",
            build_main_menu()
        )

    if command == "/poweron":
        if not argument:
            return telegram_response(
                "🟢 Select an active server to turn ON:",
                build_server_menu("poweron")
            )

        server, matches = find_server(argument)
        if not server:
            if matches:
                options = "\n".join(f"- {s['name']} ({s['host']})" for s in matches)
                text = f"❌ POWER ON FAILED\nMultiple matches for '{argument}':\n{options}"
            else:
                text = f"❌ POWER ON FAILED\nServer not found: {argument}"
            return telegram_response(text, build_server_menu("poweron"))

        return telegram_response(manual_power_on(server), build_main_menu())

    if command == "/poweroff":
        if not argument:
            return telegram_response(
                "🔴 Select an active server to turn OFF:",
                build_server_menu("poweroff")
            )

        server, matches = find_server(argument)
        if not server:
            if matches:
                options = "\n".join(f"- {s['name']} ({s['host']})" for s in matches)
                text = f"❌ POWER OFF FAILED\nMultiple matches for '{argument}':\n{options}"
            else:
                text = f"❌ POWER OFF FAILED\nServer not found: {argument}"
            return telegram_response(text, build_server_menu("poweroff"))

        return telegram_response(manual_power_off(server), build_main_menu())

    if command in ("/sendgraph", "/send"):
        arg = argument.lower().strip()
        if command == "/send" and arg != "graph":
            return telegram_response(
                "❌ GRAPH COMMAND FAILED\nUse /sendgraph or /send graph.",
                build_main_menu()
            )
        if command == "/sendgraph" and arg:
            return telegram_response(
                "❌ GRAPH COMMAND FAILED\nUse /sendgraph without an argument.",
                build_main_menu()
            )

        result = send_adhoc_sla_graph()
        return telegram_response(result, build_main_menu())

    if command in ("/summary", "/summarystatus", "/allstatus"):
        return telegram_response(manual_power_summary(), build_main_menu())

    return telegram_response(
        "❌ COMMAND FAILED\nUnknown command. Use /menu or /help.",
        build_main_menu()
    )

def handle_telegram_callback(callback_data):
    if callback_data == "menu:main":
        return telegram_response("🖥 SERVER CONTROL PANEL\nSelect an action:", build_main_menu())
    if callback_data == "menu:status":
        return telegram_response("📋 Select an active server to check:", build_server_menu("status"))
    if callback_data == "menu:poweron":
        return telegram_response("🟢 Select an active server to turn ON:", build_server_menu("poweron"))
    if callback_data == "menu:poweroff":
        return telegram_response("🔴 Select an active server to turn OFF:", build_server_menu("poweroff"))
    if callback_data == "action:help":
        return telegram_response(build_telegram_help(), build_main_menu())
    if callback_data == "action:schedule":
        return telegram_response(build_schedule_summary(), build_main_menu())
    if callback_data == "action:summary":
        return telegram_response(manual_power_summary(), build_main_menu())
    if callback_data == "action:graph":
        return telegram_response(send_adhoc_sla_graph(), build_main_menu())

    action, separator, value = callback_data.partition(":")
    if not separator or action not in ("status", "poweron", "poweroff"):
        return telegram_response("❌ COMMAND FAILED\nInvalid or expired button.", build_main_menu())

    server = get_active_server_by_callback_token(value)
    if not server:
        return telegram_response("❌ COMMAND FAILED\nServer button is invalid or expired.", build_main_menu())

    if action == "status":
        status = get_live_server_status(server)
        return telegram_response(
            f"✅ STATUS CHECK COMPLETED\n{server['name']} ({server['host']}) : {status}",
            build_main_menu()
        )
    if action == "poweron":
        return telegram_response(manual_power_on(server), build_main_menu())
    return telegram_response(manual_power_off(server), build_main_menu())

def send_telegram_response(response, chat_id=None):
    if not response:
        response = telegram_response("❌ COMMAND FAILED\nNo command result was returned.", build_main_menu())
    return send_telegram(
        response.get("text", "❌ COMMAND FAILED\nInvalid command response."),
        reply_markup=response.get("reply_markup"),
        chat_id=chat_id
    )

def edit_telegram_response(response, chat_id, message_id):
    if not response:
        return False
    return edit_telegram_message(
        chat_id,
        message_id,
        response.get("text", "❌ COMMAND FAILED\nInvalid command response."),
        reply_markup=response.get("reply_markup")
    )

def save_last_telegram_update_id(update_id):
    latest_state = load_json(STATE_FILE)
    latest_state["_last_telegram_update_id"] = update_id
    save_json(STATE_FILE, latest_state)

def process_telegram_commands():
    state = load_json(STATE_FILE)
    last_update_id = state.get("_last_telegram_update_id")

    if last_update_id is None:
        backlog = get_telegram_updates(timeout=0)
        if backlog:
            save_last_telegram_update_id(backlog[-1]["update_id"])
            log("Telegram command listener initialized. Existing backlog skipped.")
        return

    updates = get_telegram_updates(offset=last_update_id + 1, timeout=0)
    if not updates:
        return

    for update in updates:
        update_id = update.get("update_id")
        if update_id is None:
            continue

        last_update_id = max(last_update_id, update_id)
        # Record the update before executing a power action so a restart cannot
        # replay the same Telegram command.
        save_last_telegram_update_id(last_update_id)

        callback = update.get("callback_query")
        if callback:
            callback_id = callback.get("id")
            callback_data = (callback.get("data") or "").strip()
            message = callback.get("message") or {}
            chat_id = str(message.get("chat", {}).get("id", ""))
            message_id = message.get("message_id")

            if chat_id != str(TELEGRAM_CHAT_ID):
                log(f"TG IGNORE unauthorized callback chat_id={chat_id}")
                if callback_id:
                    answer_telegram_callback(callback_id, "Unauthorized", show_alert=True)
                continue

            is_action = callback_data.startswith(("status:", "poweron:", "poweroff:")) or callback_data in (
                "action:summary", "action:graph"
            )
            is_navigation = callback_data.startswith("menu:") or callback_data in (
                "action:help", "action:schedule"
            )

            if callback_id:
                answer_telegram_callback(
                    callback_id,
                    "Processing..." if is_action else None
                )

            try:
                response = handle_telegram_callback(callback_data)
            except Exception as e:
                log(
                    f"TG CALLBACK COMMAND ERROR data={callback_data!r} "
                    f"type={compact_telegram_error(e)} detail={safe_telegram_log_text(e)}"
                )
                response = telegram_response(
                    f"❌ COMMAND FAILED\nError: {type(e).__name__}. Check the server log.",
                    build_main_menu()
                )

            if is_navigation and message_id:
                edited = edit_telegram_response(response, chat_id, message_id)
                if not edited:
                    send_telegram_response(response, chat_id=chat_id)
            else:
                send_telegram_response(response, chat_id=chat_id)
            continue

        message = update.get("message") or update.get("edited_message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        if chat_id != str(TELEGRAM_CHAT_ID):
            log(f"TG IGNORE unauthorized chat_id={chat_id}")
            continue

        if not text:
            send_telegram_response(
                telegram_response("❌ COMMAND FAILED\nSend a text command or use /menu.", build_main_menu()),
                chat_id=chat_id
            )
            continue

        command = text.split(maxsplit=1)[0].lower()

        try:
            response = handle_telegram_command(text)
        except Exception as e:
            log(
                f"TG TEXT COMMAND ERROR command={command!r} "
                f"type={compact_telegram_error(e)} detail={safe_telegram_log_text(e)}"
            )
            response = telegram_response(
                f"❌ COMMAND FAILED\nError: {type(e).__name__}. Check the server log.",
                build_main_menu()
            )

        send_telegram_response(response, chat_id=chat_id)

def notify_power_change(server, host, previous_state, current_state):
    send_telegram(
        f"🔄 POWER STATE CHANGE DETECTED\n"
        f"{server['name']} ({host})\n"
        f"{previous_state} -> {current_state}"
    )

# -------- RETRY --------
def retry_power_on(sh, server, state):
    host = server["host"]
    last = state.get(host, {}).get("last_attempt")

    if last:
        last_dt = datetime.fromisoformat(last)
        if datetime.now() - last_dt < timedelta(minutes=COOLDOWN_MINUTES):
            log(f"[{host}] retry_power_on skipped due to cooldown window")
            return "COOLDOWN"

    success = False
    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[{host}] retry_power_on attempt {attempt}/{MAX_RETRIES}")
        power_on(sh, server)
        time.sleep(VERIFY_DELAY)
        after = get_power(sh, server)
        log(f"[{host}] retry_power_on post-check state = {after}")
        if after == "ON":
            success = True
            break

    state.setdefault(host, {})["last_attempt"] = datetime.now().isoformat()
    return "ON" if success else "FAILED"

# -------- METRICS --------
def update_metrics(metrics, host, st):
    now = datetime.now()

    data = metrics.setdefault(host, {
        "last": None,
        "t": now.isoformat(),
        "up": 0,
        "down": 0,
        "f": 0,
        "r": 0,
        "timeline": []
    })

    elapsed = (now - datetime.fromisoformat(data["t"])).total_seconds()

    if data["last"] == "ON":
        data["up"] += elapsed
    elif data["last"] == "OFF":
        data["down"] += elapsed

    if st != data["last"]:
        data["timeline"].append({"time": now.isoformat(), "state": st})
        data["t"] = now.isoformat()

    if st == "FAILED":
        data["f"] += 1
    elif st == "ON" and data["last"] == "OFF":
        data["r"] += 1

    data["last"] = st

# -------- GRAPH --------
def generate_combined_graph(metrics):
    plt.figure()

    for host, data in metrics.items():
        times, states = [], []
        for e in data.get("timeline", []):
            times.append(datetime.fromisoformat(e["time"]))
            states.append(1 if e["state"] == "ON" else 0)

        if times:
            plt.step(times, states, where="post", label=host)

    plt.yticks([0, 1], ["OFF", "ON"])
    plt.xlabel("Time")
    plt.ylabel("State")
    plt.title("Server Timeline (All Servers)")
    plt.xticks(rotation=45)
    plt.legend()

    file = "/tmp/combined_sla.png"
    plt.tight_layout()
    plt.savefig(file)
    plt.close()
    return file


def get_server_name_by_host(host):
    for server in SERVERS:
        if str(server.get("host")) == str(host):
            return server.get("name", "")
    return ""

def generate_detailed_sla_csv(metrics, generated_at):
    safe_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"/tmp/adhoc_server_sla_details_{safe_stamp}.csv"

    fieldnames = [
        "generated_at",
        "row_type",
        "server_name",
        "host",
        "last_state",
        "uptime_seconds",
        "downtime_seconds",
        "uptime_percent",
        "downtime_minutes",
        "recovery_count",
        "failure_count",
        "timeline_event_time",
        "timeline_event_state"
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for host, data in metrics.items():
            if not isinstance(data, dict):
                continue

            up = float(data.get("up", 0) or 0)
            down = float(data.get("down", 0) or 0)
            total = up + down
            uptime = (up / total * 100) if total else 0
            timeline = data.get("timeline", []) or []
            last_state = data.get("last") or (timeline[-1].get("state") if timeline else "UNKNOWN")

            writer.writerow({
                "generated_at": generated_at,
                "row_type": "summary",
                "server_name": get_server_name_by_host(host),
                "host": host,
                "last_state": last_state,
                "uptime_seconds": int(up),
                "downtime_seconds": int(down),
                "uptime_percent": f"{uptime:.2f}",
                "downtime_minutes": int(down / 60),
                "recovery_count": int(data.get("r", 0) or 0),
                "failure_count": int(data.get("f", 0) or 0),
                "timeline_event_time": "",
                "timeline_event_state": ""
            })

            for event in timeline:
                writer.writerow({
                    "generated_at": generated_at,
                    "row_type": "timeline",
                    "server_name": get_server_name_by_host(host),
                    "host": host,
                    "last_state": last_state,
                    "uptime_seconds": int(up),
                    "downtime_seconds": int(down),
                    "uptime_percent": f"{uptime:.2f}",
                    "downtime_minutes": int(down / 60),
                    "recovery_count": int(data.get("r", 0) or 0),
                    "failure_count": int(data.get("f", 0) or 0),
                    "timeline_event_time": event.get("time", ""),
                    "timeline_event_state": event.get("state", "")
                })

    return path

def build_adhoc_sla_email_body(metrics, generated_at):
    lines = [
        "ICTU AD HOC SERVER SLA REPORT",
        f"Generated: {generated_at}",
        "Requested through Telegram command: /sendgraph",
        "",
        "Attached files:",
        "1. Current server SLA graph",
        "2. Detailed SLA CSV file",
        "",
        "Summary:"
    ]

    for host, data in metrics.items():
        if not isinstance(data, dict):
            continue

        up = float(data.get("up", 0) or 0)
        down = float(data.get("down", 0) or 0)
        total = up + down
        uptime = (up / total * 100) if total else 0
        server_name = get_server_name_by_host(host) or host
        last_state = data.get("last", "UNKNOWN")

        lines.append(
            f"- {server_name} ({host}) | "
            f"Last State: {last_state} | "
            f"Uptime: {uptime:.2f}% | "
            f"Down: {int(down / 60)}m | "
            f"Recoveries: {int(data.get('r', 0) or 0)} | "
            f"Failures: {int(data.get('f', 0) or 0)}"
        )

    lines.extend([
        "",
        "Note: This ad hoc extraction does not reset the monthly SLA metrics."
    ])

    return "\n".join(lines)

# -------- PDF --------
def generate_pdf_report(metrics, graph):
    path = "/tmp/sla_report.pdf"
    doc = SimpleDocTemplate(path)
    styles = getSampleStyleSheet()
    elements = []

    month = datetime.now().strftime("%B %Y")
    elements.append(Paragraph(f"ICTU Monthly SLA Report - {month}", styles["Title"]))
    elements.append(Spacer(1, 12))

    for h, d in metrics.items():
        total = d["up"] + d["down"]
        uptime = (d["up"] / total * 100) if total else 0

        elements.append(Paragraph(
            f"<b>{h}</b><br/>"
            f"Uptime:{uptime:.2f}%<br/>"
            f"Down:{int(d['down']/60)}m<br/>"
            f"Rec:{d['r']} Fail:{d['f']}",
            styles["Normal"]
        ))
        elements.append(Spacer(1, 10))

    if graph and os.path.exists(graph):
        elements.append(Spacer(1, 20))
        elements.append(Image(graph, width=500, height=250))

    doc.build(elements)
    return path

# -------- SLA --------
def send_sla():
    metrics = load_json(METRICS_FILE)
    attachments = []
    body = []

    month = datetime.now().strftime("%B %Y")
    body.append(f"ICTU MONTHLY SLA REPORT - {month}\n")

    try:
        for h, d in metrics.items():
            total = d["up"] + d["down"]
            uptime = (d["up"] / total * 100) if total else 0

            body.append(
                f"{h} Uptime:{uptime:.2f}% "
                f"Down:{int(d['down']/60)}m "
                f"Rec:{d['r']} Fail:{d['f']}"
            )

        graph = generate_combined_graph(metrics)
        attachments.append(graph)

        pdf = generate_pdf_report(metrics, graph)
        attachments.append(pdf)

        send_email(
            f"Monthly SLA Report - {month}",
            "\n".join(body),
            attachments
        )

    finally:
        for f in attachments:
            try:
                if os.path.exists(f):
                    os.remove(f)
                    log(f"Deleted file: {f}")
            except Exception as e:
                log(f"DELETE ERROR {f}: {e}")

    save_json(METRICS_FILE, {})

def send_adhoc_sla_graph():
    metrics = load_json(METRICS_FILE)
    attachments = []

    if not metrics:
        return (
            "⚠️ AD HOC SLA GRAPH\n"
            "No SLA metrics found yet. Please wait until the monitoring script records server state data."
        )

    try:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        email_subject = f"ICTU Ad Hoc Server SLA Report - {generated_at}"

        graph = generate_combined_graph(metrics)
        csv_file = generate_detailed_sla_csv(metrics, generated_at)
        attachments.extend([graph, csv_file])

        # Telegram receives graph only.
        graph_sent = send_telegram_photo(
            graph,
            caption=(
                "📊 AD HOC SERVER SLA GRAPH\n"
                f"Generated: {generated_at}"
            )
        )

        # Email receives graph plus detailed CSV file only.
        email_body = build_adhoc_sla_email_body(metrics, generated_at)
        email_sent = send_email(
            email_subject,
            email_body,
            attachments=[graph, csv_file]
        )

        if graph_sent and email_sent:
            log("AD HOC SLA GRAPH SENT TO TELEGRAM AND EMAIL WITH CSV")
            return (
                "✅ SLA GRAPH COMMAND SUCCESS\n"
                "The graph was sent to Telegram and the graph with CSV details was emailed."
            )

        failed_destinations = []
        if not graph_sent:
            failed_destinations.append("Telegram graph")
        if not email_sent:
            failed_destinations.append("email report")
        return "❌ SLA GRAPH COMMAND FAILED\nFailed: " + ", ".join(failed_destinations)

    except Exception as e:
        log(f"AD HOC SLA GRAPH / EMAIL ERROR {e}")
        return f"⚠️ AD HOC SLA GRAPH / EMAIL ERROR\n{e}"

    finally:
        for f in attachments:
            try:
                if os.path.exists(f):
                    os.remove(f)
                    log(f"Deleted ad hoc file: {f}")
            except Exception as e:
                log(f"AD HOC DELETE ERROR {f}: {e}")

# -------- PROCESS --------
def initialize_server_silently(server, state, metrics):
    host = server["host"]
    c = None
    try:
        c, sh = open_shell(server)
        curr = get_power(sh, server)

        state.setdefault(host, {})["last_state"] = curr
        state.setdefault(host, {})["last_observed_power"] = curr
        state.setdefault(host, {})["initialized"] = True

        update_metrics(metrics, host, curr)
        log(f"[INIT][{host}] silent state detect = {curr}")

    except Exception as e:
        log(f"[INIT][{host}] ERROR {e}")
    finally:
        if c:
            c.close()

def process_server(server, state, metrics):
    host = server["host"]
    c = None

    try:
        c, sh = open_shell(server)
        curr = get_power(sh, server)
        previous_state = state.get(host, {}).get("last_observed_power")

        log(f"[{host}] recovery monitor state = {curr}")

        if curr == "UNKNOWN":
            final_state = "UNKNOWN"
            if previous_state != "UNKNOWN":
                send_telegram(
                    f"⚠️ RECOVERY STATUS UNKNOWN\n"
                    f"{server['name']} ({host})\n"
                    f"Unable to verify current power state."
                )
        else:
            if previous_state in ("ON", "OFF") and curr != previous_state:
                log(f"[{host}] power state changed from {previous_state} to {curr} during recovery window")
                notify_power_change(server, host, previous_state, curr)

            if curr == "OFF":
                log(f"[{host}] OFF detected during recovery monitor, attempting power on")
                result = retry_power_on(sh, server, state)

                if result == "ON":
                    final_state = "ON"
                    send_telegram(f"🟢 SERVER RECOVERED / POWERED ON\n{server['name']} ({host})")
                elif result == "COOLDOWN":
                    final_state = "OFF"
                    log(f"[{host}] COOLDOWN active, skipping power on")
                else:
                    final_state = "FAILED"
                    send_telegram(f"⚠️ SERVER POWER ON FAILED\n{server['name']} ({host})")
            else:
                final_state = "ON"
                log(f"[{host}] already ON, no action")

        actual_state = (
            "ON" if final_state == "ON"
            else "OFF" if final_state in ("OFF", "FAILED")
            else "UNKNOWN"
        )

        state.setdefault(host, {})["last_state"] = actual_state
        state.setdefault(host, {})["last_observed_power"] = actual_state
        state.setdefault(host, {})["initialized"] = True
        update_metrics(metrics, host, final_state)

    except Exception as e:
        log(f"[{host}] PROCESS ERROR {e}")
        send_telegram(f"⚠️ RECOVERY MONITOR ERROR\n{server['name']} ({host})\n{e}")
    finally:
        if c:
            c.close()

def process_server_power_on_morning(server, state, metrics, schedule_label):
    host = server["host"]
    c = None

    try:
        c, sh = open_shell(server)
        curr = get_power(sh, server)
        log(f"[{host}] {schedule_label} pre-check state = {curr}")

        if curr == "UNKNOWN":
            log(f"[{host}] {schedule_label} first read is UNKNOWN, retrying power-state read once")
            time.sleep(2)
            curr = get_power(sh, server)
            log(f"[{host}] {schedule_label} second read state = {curr}")

        if curr == "UNKNOWN":
            final_state = "UNKNOWN"
            send_telegram(
                f"⚠️ {schedule_label} POWER STATUS UNKNOWN\n"
                f"{server['name']} ({host})\n"
                f"Unable to verify current power state."
            )
        elif curr == "OFF":
            log(f"[{host}] {schedule_label} power-on sequence")
            result = retry_power_on(sh, server, state)

            if result == "ON":
                final_state = "ON"
                send_telegram(f"🟢 {schedule_label} POWER ON SUCCESS\n{server['name']} ({host})")
            elif result == "COOLDOWN":
                final_state = "OFF"
                log(f"[{host}] COOLDOWN active during {schedule_label} power-on")
                send_telegram(f"ℹ️ {schedule_label} POWER ON SKIPPED (COOLDOWN)\n{server['name']} ({host})")
            else:
                final_state = "FAILED"
                send_telegram(f"⚠️ {schedule_label} POWER ON FAILED\n{server['name']} ({host})")
        else:
            final_state = "ON"
            log(f"[{host}] {schedule_label} already ON")
            send_telegram(f"ℹ️ {schedule_label} SERVER ALREADY ON\n{server['name']} ({host})")

        actual_state = (
            "ON" if final_state == "ON"
            else "OFF" if final_state in ("OFF", "FAILED")
            else "UNKNOWN"
        )

        state.setdefault(host, {})["last_state"] = actual_state
        state.setdefault(host, {})["last_observed_power"] = actual_state
        state.setdefault(host, {})["initialized"] = True
        update_metrics(metrics, host, final_state)

    except Exception as e:
        log(f"[{host}] {schedule_label} ERROR {e}")
        send_telegram(f"⚠️ {schedule_label} POWER ON ERROR\n{server['name']} ({host})\n{e}")
    finally:
        if c:
            c.close()

def refresh_server_state(server, state, metrics):
    host = server["host"]
    c = None

    try:
        c, sh = open_shell(server)
        curr = get_power(sh, server)

        state.setdefault(host, {})["last_state"] = curr
        state.setdefault(host, {})["last_observed_power"] = curr
        state.setdefault(host, {})["initialized"] = True
        update_metrics(metrics, host, curr)

        log(f"[{host}] verified state = {curr}")

    except Exception as e:
        log(f"[{host}] VERIFY ERROR {e}")
        state.setdefault(host, {})["last_state"] = "ERROR"
        state.setdefault(host, {})["last_observed_power"] = "ERROR"
    finally:
        if c:
            c.close()

def send_status_summary(title):
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda s: refresh_server_state(s, st, mt), ACTIVE_SERVERS))
    except Exception as e:
        log(f"SUMMARY REFRESH ERROR {e}")
    finally:
        save_json(STATE_FILE, st)
        save_json(METRICS_FILE, mt)

    lines = [title]
    for server in ACTIVE_SERVERS:
        host = server["host"]
        name = server["name"]
        current = st.get(host, {}).get("last_state", "UNKNOWN")
        lines.append(f"{name} ({host}) : {current}")

    message = "\n".join(lines)
    log(message)
    send_telegram(message)

def send_final_status(schedule_label):
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda s: refresh_server_state(s, st, mt), ACTIVE_SERVERS))
    except Exception as e:
        log(f"{schedule_label} STATUS REFRESH ERROR {e}")
    finally:
        save_json(STATE_FILE, st)
        save_json(METRICS_FILE, mt)

    lines = [f"📋 {schedule_label} FINAL SERVER STATUS"]
    for server in ACTIVE_SERVERS:
        host = server["host"]
        name = server["name"]
        current = st.get(host, {}).get("last_state", "UNKNOWN")
        lines.append(f"{name} ({host}) : {current}")

    message = "\n".join(lines)
    log(message)
    send_telegram(message)

def process_server_force_off(server, state, metrics):
    host = server["host"]
    c = None

    try:
        c, sh = open_shell(server)

        curr = get_power(sh, server)
        log(f"[{host}] force-off pre-check state = {curr}")

        if curr == "UNKNOWN":
            final_state = "UNKNOWN"
            send_telegram(
                f"⚠️ FORCE OFF STATUS UNKNOWN\n"
                f"{server['name']} ({host})\n"
                f"Unable to verify current power state. Force OFF skipped."
            )
            state.setdefault(host, {})["last_state"] = final_state
            state.setdefault(host, {})["last_observed_power"] = final_state
            state.setdefault(host, {})["initialized"] = True
            update_metrics(metrics, host, final_state)
            return

        if curr != "ON":
            final_state = "OFF"
            log(f"[{host}] already OFF at force-off checkpoint, no force OFF command sent")

            state.setdefault(host, {})["last_state"] = final_state
            state.setdefault(host, {})["last_observed_power"] = final_state
            state.setdefault(host, {})["initialized"] = True
            update_metrics(metrics, host, final_state)
            return

        confirm = get_power(sh, server)
        log(f"[{host}] force-off confirm state = {confirm}")

        if confirm == "UNKNOWN":
            final_state = "UNKNOWN"
            send_telegram(
                f"⚠️ FORCE OFF CONFIRM STATUS UNKNOWN\n"
                f"{server['name']} ({host})\n"
                f"Unable to verify state before force OFF."
            )
            state.setdefault(host, {})["last_state"] = final_state
            state.setdefault(host, {})["last_observed_power"] = final_state
            state.setdefault(host, {})["initialized"] = True
            update_metrics(metrics, host, final_state)
            return

        if confirm != "ON":
            final_state = "OFF"
            log(f"[{host}] second check is not ON, skipping force OFF")

            state.setdefault(host, {})["last_state"] = final_state
            state.setdefault(host, {})["last_observed_power"] = final_state
            state.setdefault(host, {})["initialized"] = True
            update_metrics(metrics, host, final_state)
            return

        log(f"[{host}] issuing force OFF command")
        power_off(sh, server)
        time.sleep(VERIFY_DELAY)

        after = get_power(sh, server)
        log(f"[{host}] force-off post-check state = {after}")

        if after == "OFF":
            final_state = "OFF"
            send_telegram(f"🔴 FORCE OFF SUCCESS\n{server['name']} ({host})")
        elif after == "ON":
            final_state = "ON"
            send_telegram(f"⚠️ FORCE OFF FAILED\n{server['name']} ({host})")
        else:
            final_state = "UNKNOWN"
            send_telegram(
                f"⚠️ FORCE OFF POST-CHECK UNKNOWN\n"
                f"{server['name']} ({host})\n"
                f"Unable to verify final power state."
            )

        state.setdefault(host, {})["last_state"] = final_state
        state.setdefault(host, {})["last_observed_power"] = final_state
        state.setdefault(host, {})["initialized"] = True
        update_metrics(metrics, host, final_state)

    except Exception as e:
        log(f"[{host}] FORCE OFF ERROR {e}")
        send_telegram(f"⚠️ FORCE OFF ERROR\n{server['name']} ({host})\n{e}")
    finally:
        if c:
            c.close()

# -------- CONTROL --------
def initialize_all_silently():
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda s: initialize_server_silently(s, st, mt), ACTIVE_SERVERS))

    st["_script_initialized"] = True
    st["_first_monitor_pending"] = True

    save_json(STATE_FILE, st)
    save_json(METRICS_FILE, mt)

def monitor():
    log("RECOVERY MONITOR TICK STARTED")
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda s: process_server(s, st, mt), ACTIVE_SERVERS))

    st["_first_monitor_pending"] = False
    save_json(STATE_FILE, st)
    save_json(METRICS_FILE, mt)
    log("RECOVERY MONITOR TICK COMPLETED")

def refresh_all_states():
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda s: refresh_server_state(s, st, mt), ACTIVE_SERVERS))

    save_json(STATE_FILE, st)
    save_json(METRICS_FILE, mt)

def morning_power_on_all(schedule_label):
    log(f"{schedule_label} MORNING POWER-ON RUN STARTED")
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(
            lambda s: process_server_power_on_morning(s, st, mt, schedule_label),
            ACTIVE_SERVERS
        ))

    save_json(STATE_FILE, st)
    save_json(METRICS_FILE, mt)
    log(f"{schedule_label} MORNING POWER-ON RUN COMPLETED")

def force_off_all():
    log("FORCE OFF RUN STARTED")
    st = load_json(STATE_FILE)
    mt = load_json(METRICS_FILE)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda s: process_server_force_off(s, st, mt), ACTIVE_SERVERS))

    save_json(STATE_FILE, st)
    save_json(METRICS_FILE, mt)
    log("FORCE OFF RUN COMPLETED")

# -------- MAIN --------
if __name__ == "__main__":
    log_disabled_servers()
    log_active_servers()
    state = load_json(STATE_FILE)

    if not state.get("_script_initialized"):
        initialize_all_silently()

    while True:
        now = datetime.now()
        process_telegram_commands()
        state = load_json(STATE_FILE)

        if should_send_sla(now) and not ran_today(state, "_last_sla_date", now):
            send_sla()
            state = load_json(STATE_FILE)
            mark_ran_today(state, "_last_sla_date", now)
            save_json(STATE_FILE, state)

        if is_weekday(now):
            day_group = "WEEKDAY"
            power_on_time = WEEKDAY_POWER_ON_TIME
            recovery_start = WEEKDAY_RECOVERY_START_TIME
            recovery_end = WEEKDAY_RECOVERY_END_TIME
            force_monitor_start = WEEKDAY_FORCE_MONITOR_START_TIME
            force_off_time = WEEKDAY_FORCE_OFF_TIME
        else:
            day_group = "WEEKEND"
            power_on_time = WEEKEND_POWER_ON_TIME
            recovery_start = WEEKEND_RECOVERY_START_TIME
            recovery_end = WEEKEND_RECOVERY_END_TIME
            force_monitor_start = WEEKEND_FORCE_MONITOR_START_TIME
            force_off_time = WEEKEND_FORCE_OFF_TIME

        power_on_label = format_schedule_time(power_on_time)
        recovery_start_label = format_schedule_time(recovery_start)
        recovery_end_label = format_schedule_time(recovery_end)
        force_monitor_label = format_schedule_time(force_monitor_start)
        force_off_label = format_schedule_time(force_off_time)

        # One scheduled morning power-on per day.
        if is_power_on_window(now) and not ran_today(state, "_last_morning_power_on_date", now):
            log(f"{power_on_label} checkpoint reached: starting scheduled power on")
            send_telegram(
                f"🟢 {power_on_label} {day_group} SCHEDULED POWER ON STARTED\n"
                "Processing enabled servers now."
            )
            morning_power_on_all(power_on_label)

            state = load_json(STATE_FILE)
            mark_ran_today(state, "_last_morning_power_on_date", now)
            save_json(STATE_FILE, state)

        # Refresh the clock after power operations, which can take several minutes.
        now = datetime.now()
        state = load_json(STATE_FILE)

        if is_recovery_window(now):
            if not ran_today(state, "_last_recovery_start_notice_date", now):
                log(f"{recovery_start_label} checkpoint reached: recovery monitoring is active")
                send_telegram(
                    f"🟢 {day_group} RECOVERY MONITORING ACTIVE\n"
                    f"Window: {recovery_start_label} - {recovery_end_label}\n"
                    "Checking enabled servers every 30 minutes."
                )
                mark_ran_today(state, "_last_recovery_start_notice_date", now)
                save_json(STATE_FILE, state)

            recovery_slot = schedule_slot_key(now)
            if state.get("_last_recovery_monitor_slot") != recovery_slot:
                monitor()
                state = load_json(STATE_FILE)
                state["_last_recovery_monitor_slot"] = recovery_slot
                save_json(STATE_FILE, state)

        # Refresh the clock in case recovery checks were slow.
        now = datetime.now()
        state = load_json(STATE_FILE)

        if is_force_monitor_window(now):
            if not ran_today(state, "_last_force_monitor_start_notice_date", now):
                log(f"{force_monitor_label} checkpoint reached: force-off monitoring is active")
                send_telegram(
                    f"🟠 {day_group} FORCE-OFF MONITORING ACTIVE\n"
                    f"Window: {force_monitor_label} - {force_off_label}\n"
                    "Refreshing enabled server states before scheduled force shutdown."
                )
                mark_ran_today(state, "_last_force_monitor_start_notice_date", now)
                save_json(STATE_FILE, state)

            force_monitor_slot = schedule_slot_key(now)
            if state.get("_last_force_monitor_slot") != force_monitor_slot:
                refresh_all_states()
                state = load_json(STATE_FILE)
                state["_last_force_monitor_slot"] = force_monitor_slot
                save_json(STATE_FILE, state)

        # Refresh again so a slow state check cannot postpone the shutdown.
        now = datetime.now()
        state = load_json(STATE_FILE)

        if is_force_off_window(now):
            if not ran_today(state, "_last_force_off_start_notice_date", now):
                log(f"{force_off_label} checkpoint reached: starting scheduled force shutdown")
                send_telegram(
                    f"🔴 {force_off_label} {day_group} SCHEDULED FORCE SHUTDOWN STARTED\n"
                    "Processing enabled servers now."
                )
                mark_ran_today(state, "_last_force_off_start_notice_date", now)
                save_json(STATE_FILE, state)

            if not ran_today(state, "_last_force_off_date", now):
                force_off_all()
                state = load_json(STATE_FILE)
                mark_ran_today(state, "_last_force_off_date", now)
                save_json(STATE_FILE, state)

        # Send one final status after the scheduled shutdown for the current day group.
        now = datetime.now()
        state = load_json(STATE_FILE)
        final_summary_time = (
            WEEKDAY_FINAL_SUMMARY_TIME if is_weekday(now)
            else WEEKEND_FINAL_SUMMARY_TIME
        )
        if now.time() >= final_summary_time:
            if not ran_today(state, "_last_final_summary_date", now):
                final_label = format_schedule_time(final_summary_time)
                log(f"{final_label} checkpoint reached: sending final server status")
                send_final_status(final_label)
                state = load_json(STATE_FILE)
                mark_ran_today(state, "_last_final_summary_date", now)
                save_json(STATE_FILE, state)

        sleep_until_next_wakeup()
