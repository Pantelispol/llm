from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql://tourist:tourist@localhost:5432/tourist"
    llm_model: str = ""
    llm_api_key: str = Field(default="", repr=False)
    ors_api_key: str = Field(default="", repr=False)
    weather_fixture: str | None = None
    prompt_version: str = "understand.v1,narrator.v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
