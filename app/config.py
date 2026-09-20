from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
LLMMode = Literal["live", "record", "replay"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = ""
    rag_store: Literal["pgvector", "memory"] = "pgvector"
    llm_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    llm_mode: LLMMode = "replay"
    llm_max_output_tokens: int = Field(default=512, ge=1, le=128_000)
    understand_model: Literal["gpt-5.6-luna"] = "gpt-5.6-luna"
    understand_reasoning_effort: ReasoningEffort = "none"
    narrate_model: Literal["gpt-5.6-luna", "gpt-5.6-terra"] = "gpt-5.6-luna"
    narrate_reasoning_effort: ReasoningEffort = "low"
    ors_api_key: str = Field(default="", repr=False)
    weather_fixture: str | None = None
    weather_cache_ttl_seconds: int = Field(default=1200, gt=0)
    weather_rain_probability_threshold: int = Field(default=50, ge=0, le=100)
    weather_heat_apparent_c: float = 35.0
    weather_heat_children_apparent_c: float = 32.0
    weather_uv_high_threshold: float = Field(default=8.0, ge=0)
    planner_top_k: int = Field(default=10, ge=1, le=25)
    planner_beam_width: int = Field(default=30, ge=1, le=100)
    planner_interest_weight: float = Field(default=4.0, ge=0)
    planner_must_see_weight: float = Field(default=2.0, ge=0)
    planner_weather_weight: float = Field(default=4.0, ge=0)
    planner_child_weight: float = Field(default=4.0, ge=0)
    planner_diversity_weight: float = Field(default=1.0, ge=0)
    planner_travel_weight: float = Field(default=0.02, ge=0)
    planner_perturbation_weight: float = Field(default=3.0, ge=0)
    planner_utilization_weight: float = Field(default=12.0, ge=0)
    planner_child_walk_soft_minutes: int = Field(default=15, ge=0)
    planner_child_walk_hard_minutes: int = Field(default=25, ge=1)
    planner_child_walk_penalty: float = Field(default=0.4, ge=0)
    planner_child_hilly_penalty: float = Field(default=3.0, ge=0)
    default_day_start_hour: int = Field(default=9, ge=0, le=23)
    retrieval_limit: int = Field(default=4, ge=1, le=20)
    prompt_version: str = "understand.v1,narrate.v2"

    @model_validator(mode="after")
    def live_modes_require_llm_api_key(self) -> Settings:
        if self.llm_mode in {"live", "record"} and not self.llm_api_key.get_secret_value():
            raise ValueError("LLM_API_KEY is required when LLM_MODE is live or record")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
