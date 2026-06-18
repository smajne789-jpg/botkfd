import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    config = {}
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token", ""),
        "crypto_pay_api_token": os.getenv("CRYPTO_PAY_API_TOKEN") or config.get("crypto_pay_api_token", ""),
    }


CONFIG = load_config()
TELEGRAM_BOT_TOKEN = CONFIG["telegram_bot_token"]
CRYPTO_PAY_API_TOKEN = CONFIG["crypto_pay_api_token"]
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
CRYPTO_PAY_API_BASE = "https://pay.crypt.bot/api"


def save_config(telegram_bot_token: str, crypto_pay_api_token: str) -> None:
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "telegram_bot_token": telegram_bot_token,
                "crypto_pay_api_token": crypto_pay_api_token,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def ensure_config() -> tuple[str, str]:
    telegram_bot_token = TELEGRAM_BOT_TOKEN.strip()
    crypto_pay_api_token = CRYPTO_PAY_API_TOKEN.strip()

    if telegram_bot_token and crypto_pay_api_token:
        return telegram_bot_token, crypto_pay_api_token

    print("Нужна первичная настройка бота.")
    print("Если токен уже есть, просто вставьте его и нажмите Enter.")

    if not telegram_bot_token:
        telegram_bot_token = input("Введите Telegram bot token: ").strip()
    if not crypto_pay_api_token:
        crypto_pay_api_token = input("Введите Crypto Pay API token: ").strip()

    missing = []
    if not telegram_bot_token:
        missing.append("telegram_bot_token")
    if not crypto_pay_api_token:
        missing.append("crypto_pay_api_token")
    if missing:
        print("Не заполнены поля: " + ", ".join(missing))
        sys.exit(1)

    save_config(telegram_bot_token, crypto_pay_api_token)
    print(f"Токены сохранены в {CONFIG_PATH.name}.")
    return telegram_bot_token, crypto_pay_api_token


def http_json(url: str, *, headers: dict | None = None, params: dict | None = None) -> dict:
    if params:
        query = urllib.parse.urlencode(params)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"

    request_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def crypto_pay(method: str, **params) -> dict:
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    data = http_json(f"{CRYPTO_PAY_API_BASE}/{method}", headers=headers, params=params or None)
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Crypto Pay API returned an error"))
    return data


def telegram(method: str, **params) -> dict:
    data = http_json(f"{TELEGRAM_API_BASE}/{method}", params=params or None)
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API returned an error"))
    return data


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def extract_hash(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return value

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("start", "startapp"):
            if query.get(key):
                return query[key][0]
        if parsed.fragment:
            return parsed.fragment
        return value.rstrip("/").split("/")[-1]

    if "=" in value:
        tail = value.split("=")[-1].strip()
        if tail:
            return tail

    return value


def fetch_checks(*, status: str | None = None, count: int = 20) -> list[dict]:
    params = {"count": max(1, min(count, 100))}
    if status:
        params["status"] = status
    return crypto_pay("getChecks", **params)["result"]["items"]


def find_check(search_value: str) -> dict | None:
    normalized = extract_hash(search_value)
    if not normalized:
        return None

    if normalized.isdigit():
        items = crypto_pay("getChecks", check_ids=normalized)["result"]["items"]
        return items[0] if items else None

    for status in ("active", "activated", None):
        checks = fetch_checks(status=status, count=100)
        for check in checks:
            if str(check.get("hash", "")).strip() == normalized:
                return check
            if str(check.get("bot_check_url", "")).strip() == search_value.strip():
                return check
    return None


def format_check_line(check: dict) -> str:
    amount = check.get("amount", "?")
    asset = check.get("asset", "?")
    check_id = check.get("check_id", "?")
    status = check.get("status", "?")
    hash_value = check.get("hash", "?")
    return (
        f"ID: <code>{check_id}</code> | {escape_html(str(amount))} {escape_html(str(asset))} | "
        f"{escape_html(str(status))}\n"
        f"hash: <code>{escape_html(str(hash_value))}</code>"
    )


def help_text() -> str:
    return (
        "Команды:\n"
        "/checks - последние 10 чеков\n"
        "/active - активные чеки\n"
        "/activated - активированные чеки\n"
        "/find <check_id | hash | url> - найти чек\n\n"
        "Если у вас есть только ссылка на чек, отправьте ее в /find и бот попробует найти check_id по hash."
    )


def handle_message(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return help_text()

    if text.startswith("/start") or text.startswith("/help"):
        return help_text()

    if text.startswith("/checks"):
        checks = fetch_checks(count=10)
        if not checks:
            return "Чеки не найдены."
        return "\n\n".join(format_check_line(check) for check in checks)

    if text.startswith("/active"):
        checks = fetch_checks(status="active", count=10)
        if not checks:
            return "Активные чеки не найдены."
        return "\n\n".join(format_check_line(check) for check in checks)

    if text.startswith("/activated"):
        checks = fetch_checks(status="activated", count=10)
        if not checks:
            return "Активированные чеки не найдены."
        return "\n\n".join(format_check_line(check) for check in checks)

    if text.startswith("/find"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "Использование: /find <check_id | hash | url>"
        check = find_check(parts[1])
        if not check:
            return "Чек не найден. Попробуйте /checks, чтобы увидеть последние чеки."
        return format_check_line(check)

    return help_text()


def send_message(chat_id: int, text: str) -> None:
    telegram(
        "sendMessage",
        chat_id=str(chat_id),
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true",
    )


def poll() -> None:
    offset = 0
    print("Bot is running. Press Ctrl+C to stop.")
    while True:
        try:
            response = telegram("getUpdates", timeout="30", offset=str(offset))
            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat_id = message["chat"]["id"]
                text = message.get("text", "")
                try:
                    reply = handle_message(text)
                except Exception as exc:
                    reply = f"Ошибка: <code>{escape_html(str(exc))}</code>"
                send_message(chat_id, reply)
        except KeyboardInterrupt:
            print("Bot stopped.")
            return
        except Exception as exc:
            print(f"Polling error: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    telegram_token, crypto_token = ensure_config()
    TELEGRAM_BOT_TOKEN = telegram_token
    CRYPTO_PAY_API_TOKEN = crypto_token
    TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    poll()
