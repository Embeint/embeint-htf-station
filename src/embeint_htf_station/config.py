from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTF_", env_file=".env", extra="ignore")

    broker_host: str = "localhost"
    broker_port: int = 1883
    broker_username: str | None = None
    broker_password: str | None = None

    org_id: str = Field(..., description="UUID of the org this station belongs to")
    station_id: str = Field(..., description="UUID assigned to this station by the server")

    @property
    def topic_prefix(self) -> str:
        return f"htf/v1/{self.org_id}/{self.station_id}"
