
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    DATABASE_URL: str = Field(default="")
    REDIS_URL: str = Field(default="")
    API_KEY: str = Field(default="")
    N8N_SYSTEM_WEBHOOK_URL: str = Field(default="")
    GEOIP_CITY_DB_PATH: str = Field(default="")
    GEOIP_ASN_DB_PATH: str = Field(default="")
    # FR-145: path to the hot-reloadable warmup/limits config. Empty → the default
    # location config/safety.yaml under the project root.
    SAFETY_CONFIG_PATH: str = Field(default="")
    POSTGRES_USER: str = Field(default="fleet_user")
    POSTGRES_PASSWORD: str = Field(default="fleet_password")
    POSTGRES_DB: str = Field(default="fleet_db")
    # FR-330-333: apply human reading/typing/inter-action delays (through the Clock)
    # before each behavioural action. On in production; the test suite turns it off so
    # the inter-action floor (60-300 virtual s) doesn't sleep real minutes at scale 1.
    HUMANIZE_ACTIONS: bool = Field(default=True)

    # Proxy port auto-rotation: residential/sticky providers (e.g. puls-proxy) expose a
    # block of ports that all map to the same exit pool; when a sticky IP goes bad the
    # connection just times out. On a connection/proxy error the worker swaps the
    # account's proxy to another port in [MIN, MAX] (same login => same exit country)
    # and re-runs the task after a short backoff, up to MAX_ROTATIONS times.
    PROXY_STICKY_PORT_MIN: int = Field(default=11000)
    PROXY_STICKY_PORT_MAX: int = Field(default=11019)
    PROXY_ROTATE_BACKOFF_SECONDS: int = Field(default=20)
    PROXY_MAX_ROTATIONS: int = Field(default=6)
    # When all rotations within a cycle are exhausted, defer this long before trying the
    # whole port range again (a real provider outage shouldn't be hammered).
    PROXY_ROTATE_COOLDOWN_SECONDS: int = Field(default=1800)

    def validate(self) -> None:
        if not self.N8N_SYSTEM_WEBHOOK_URL:
            raise RuntimeError(
                "N8N_SYSTEM_WEBHOOK_URL is required and must not be empty"
            )


settings = Settings()


def get_settings() -> Settings:
    return settings
