from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class DatabaseSettings(BaseSettings):
    # Аналог @ConfigurationProperties: Pydantic читает переменные окружения
    # и .env, преобразует значения к типам и проверяет ограничения.
    # Переменные окружения имеют приоритет; extra="ignore" пропускает лишние.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "db"
    postgres_port: int = Field(default=5432, gt=0, le=65535)
    postgres_db: str = "assistant"
    postgres_user: str = "assistant"
    postgres_password: SecretStr = Field(min_length=1)
    # SecretStr маскирует значение при обычном выводе, но не шифрует его.

    @property
    def database_url(self) -> URL:
        # @property позволяет читать вычисляемое значение как поле:
        # settings.database_url без скобок (аналог getter).
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


class Settings(DatabaseSettings):
    # Наследует настройки БД и добавляет настройки бота, Google и модели.
    telegram_bot_token: SecretStr
    owner_telegram_id: int = Field(gt=0)
    user_timezone: str = "Europe/Minsk"
    google_calendar_id: str = "primary"
    google_token_path: Path = Path("secrets/google-token.json")
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = "openrouter/free"

    @field_validator("openrouter_model")
    @classmethod
    def free_model_only(cls, value: str) -> str:
        # Декораторы регистрируют проверку поля. classmethod получает класс
        # в cls вместо экземпляра в self; отдельный объект для вызова не нужен.
        if value != "openrouter/free" and not value.endswith(":free"):
            raise ValueError("Only free OpenRouter models are enabled")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def check_token(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except TokenValidationError:
            raise ValueError("Invalid Telegram token") from None
        return value

    @field_validator("user_timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        return value
