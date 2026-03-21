from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
    )

    telegram_bot_token: str
    telegram_allowed_user_id: int

    openai_api_key: str
    jina_api_key: str = ""

    obsidian_vault_path: Path
    obsidian_resource_folder: str = "Resources/Saved"
    obsidian_todo_inbox: str = "todo-inbox.md"
    session_timeout_seconds: int = 90

    pages_repo_path: Path | None = None
    pages_base_url: str = ""


settings = Settings()
