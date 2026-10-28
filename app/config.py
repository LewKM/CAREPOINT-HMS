from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    database_url: str
    secret_key: str
    app_name: str = "CarePoint HMS"
    hospital_name: str = "CarePoint General Hospital"
    currency: str = "KES"
    timezone: str = "Africa/Nairobi"
    session_max_age: int = 60 * 60 * 8  # rolling 8h shift
    https_only: bool = False


settings = Settings()
