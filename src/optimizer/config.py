from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    workload_namespace: Literal["workloads"] = "workloads"
    prometheus_url: str = "http://127.0.0.1:9090"
    observe_interval_seconds: float = Field(default=30, gt=0)
    observe_window_seconds: int = Field(default=600, ge=60)
    prometheus_timeout_seconds: float = Field(default=10, gt=0)
    recommendation_provider: Literal["openai", "fake"] = "openai"
    database_path: Path = Path("data/optimizer.db")
    issue_persistence_seconds: float = Field(default=120, ge=0)
    cpu_overprovisioned_ratio: float = Field(default=0.3, gt=0, lt=1)
    cpu_underprovisioned_ratio: float = Field(default=0.9, gt=0, le=1)
    cpu_throttling_threshold_percent: float = Field(default=20, ge=0, le=100)
    memory_risk_ratio: float = Field(default=0.85, gt=0, le=1)
    cost_mode: Literal["fixed", "azure"] = "fixed"
    azure_price_timeout_seconds: float = Field(default=10, gt=0)
    azure_price_cache_seconds: float = Field(default=3600, gt=0)
    cpu_price_per_vcpu_hour: float = Field(default=0.037, ge=0)
    memory_price_per_gib_hour: float = Field(default=0.005, ge=0)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "none"
    openai_temperature: float | None = Field(default=None, ge=0, le=2)
    openai_timeout_seconds: float = Field(default=30, gt=0)
    openai_max_output_tokens: int = Field(default=2048, ge=256)
    rollout_timeout_seconds: float = Field(default=180, gt=0)
    openai_input_price_per_million: float | None = Field(default=None, ge=0)
    openai_cached_input_price_per_million: float | None = Field(default=None, ge=0)
    openai_output_price_per_million: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_thresholds_and_prices(self) -> Self:
        if self.cpu_overprovisioned_ratio >= self.cpu_underprovisioned_ratio:
            raise ValueError("CPU over-provisioning threshold must be below under-provisioning")
        prices = (
            self.openai_input_price_per_million,
            self.openai_cached_input_price_per_million,
            self.openai_output_price_per_million,
        )
        if any(price is not None for price in prices) and not all(
            price is not None for price in prices
        ):
            raise ValueError("Set all three OpenAI token prices, or leave all unset")
        return self
