"""Run locally: uv run python -m app.google_auth."""
import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from app.calendar import SCOPES, save_token


def main() -> int:
    # Отдельная утилита авторизации на компьютере пользователя.
    # argparse читает параметры командной строки; бот этот сценарий не запускает.
    parser = argparse.ArgumentParser(description="Connect Google Calendar on this computer")
    parser.add_argument("--credentials", type=Path, default=Path("secrets/google-client.json"))
    parser.add_argument("--token", type=Path, default=Path("secrets/google-token.json"))
    args = parser.parse_args()
    if not args.credentials.is_file():
        print("Missing secrets/google-client.json. See docs/GOOGLE_CALENDAR.md.")
        return 1
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(args.credentials), SCOPES)
        credentials = flow.run_local_server(
            # Временный локальный HTTP-сервер принимает ответ браузера Google.
            # port=0 выбирает свободный порт; после согласия сохраняем токен.
            host="localhost", port=0, timeout_seconds=180,
            authorization_prompt_message="Open the Google consent page in your browser.",
            success_message="Google Calendar connected. You can close this window.",
            access_type="offline", prompt="consent",
        )
        if not credentials.refresh_token:
            print("Google did not return offline access. Please authorize again.")
            return 1
        save_token(args.token, credentials)
        print("Google Calendar connected. Token saved locally; do not share it.")
        return 0
    except Exception as exc:
        print(f"Authorization failed ({type(exc).__name__}). See docs/GOOGLE_CALENDAR.md.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
