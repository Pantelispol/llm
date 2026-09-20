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
    weather_cache_ttl_seconds: int = Field(default=1200, gt=0)
    weather_rain_probability_threshold: int = Field(default=50, ge=0, le=100)
    weather_heat_apparent_c: float = 35.0
    weather_heat_children_apparent_c: float = 32.0
    weather_uv_high_threshold: float = Field(default=8.0, ge=0)
    prompt_version: str = "understand.v1,narrator.v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
