"""
bot/config.py
─────────────
Single source of truth for all configuration loaded from the environment.

Replaces the ad-hoc ``os.environ.get(...)`` calls scattered across
``util/constants/__init__.py``.

Usage (in any cog or utility file)::

    from config import settings
    
        token = settings.bot_token.get_secret_value()
            db_url = str(settings.database_url)
            
            The ``settings`` singleton is constructed once on first import and cached.
            pydantic-settings reads ``.env`` automatically — no explicit ``load_dotenv()``
            call is needed anywhere.
            
            SecretStr fields will show as ``**********`` in logs and ``repr()`` output,
            preventing accidental token leaks.
            """
            
            # NOTE: do NOT add 'from __future__ import annotations' here.
            # Deferred annotation evaluation breaks pydantic's runtime type inspection
            # for SecretStr fields in pydantic 2.x.  Python 3.11+ evaluates standard
            # annotations eagerly by default, so the future import is unnecessary anyway.
            
            from functools import lru_cache
            from pydantic import Field, SecretStr
            from pydantic.networks import PostgresDsn
            from pydantic_settings import BaseSettings, SettingsConfigDict
            
            
            class Settings(BaseSettings):
                model_config = SettingsConfigDict(
                        # Reads .env from the project root (one level above bot/).
                                # Adjust the path if your working directory differs.
                                        env_file=".env",
                                                env_file_encoding="utf-8",
                                                        # Field names are matched case-insensitively to env var names.
                                                                case_sensitive=False,
                                                                        # Silently ignore any extra vars in .env that aren't declared here.
                                                                                extra="ignore",
                                                                                    )
                                                                                    
                                                                                        # ── Discord ──────────────────────────────────────────────────────────────
                                                                                            bot_token: SecretStr = Field(alias="BOT_TOKEN")
                                                                                                bot_debug: bool = Field(False, alias="BOT_DEBUG")
                                                                                                    # Injected by CI (git rev-parse --short HEAD). Falls back to "master".
                                                                                                        git_sha: str = Field("master", alias="GIT_SHA")
                                                                                                        
                                                                                                            # ── PostgreSQL ───────────────────────────────────────────────────────────
                                                                                                                # pydantic validates the URL format: must start with postgresql:// or postgres://
                                                                                                                    # For asyncpg, use:  str(settings.database_url).replace("postgresql://", "postgresql+asyncpg://")
                                                                                                                        # or just pass str(settings.database_url) to asyncpg.connect() directly.
                                                                                                                            database_url: PostgresDsn = Field(alias="DATABASE_URL")
                                                                                                                            
                                                                                                                                # ── Lavalink ─────────────────────────────────────────────────────────────
                                                                                                                                    lavalink_host: str = Field("localhost", alias="LAVALINK_HOST")
                                                                                                                                        lavalink_port: int = Field(2333, alias="LAVALINK_PORT")
                                                                                                                                            lavalink_password: SecretStr = Field("youshallnotpass", alias="LAVALINK_PASSWORD")
                                                                                                                                                lavalink_https: bool = Field(False, alias="LAVALINK_HTTPS")
                                                                                                                                                
                                                                                                                                                    # ── Spotify (via LavaSrc Lavalink plugin) ────────────────────────────────
                                                                                                                                                        spotify_client_id: str = Field("", alias="SPOTIFY_CLIENT_ID")
                                                                                                                                                            spotify_client_secret: SecretStr = Field("", alias="SPOTIFY_CLIENT_SECRET")
                                                                                                                                                            
                                                                                                                                                                # ── External APIs ────────────────────────────────────────────────────────
                                                                                                                                                                    # Arbitrary secret identifier used for the vote database document.
                                                                                                                                                                        secret_id: SecretStr = Field(alias="SECRET_ID")
                                                                                                                                                                            # Optional integrations — empty string = feature disabled.
                                                                                                                                                                                topgg_token: str = Field("", alias="TOPGGTOKEN")
                                                                                                                                                                                    github_token: str = Field("", alias="GITHUB_TOKEN")
                                                                                                                                                                                        
                                                                                                                                                                                        
                                                                                                                                                                                            # ── Algolia (Discord Developer Docs search) ──────────────────────────────
                                                                                                                                                                                                # Public read-only keys for the discord.dev Algolia index.
                                                                                                                                                                                                    algolia_app_id: str = Field("BH4D9OD16A", alias="ALGOLIA_SEARCH_APP_ID")
                                                                                                                                                                                                        algolia_api_key: str = Field("", alias="ALGOLIA_SEARCH_API_KEY")
                                                                                                                                                                                                            algolia_index_name: str = Field("discord", alias="ALGOLIA_SEARCH_INDEX_NAME")
                                                                                                                                                                                                            
                                                                                                                                                                                                                # ── Derived helpers ──────────────────────────────────────────────────────
                                                                                                                                                                                                                
                                                                                                                                                                                                                    @property
                                                                                                                                                                                                                        def asyncpg_dsn(self) -> str:
                                                                                                                                                                                                                                """
                                                                                                                                                                                                                                        Return a DSN string suitable for ``asyncpg.create_pool()``.
                                                                                                                                                                                                                                        
                                                                                                                                                                                                                                                asyncpg expects ``postgresql://`` (not ``postgresql+asyncpg://``).
                                                                                                                                                                                                                                                        pydantic's PostgresDsn always serialises to ``postgresql://``,
                                                                                                                                                                                                                                                                so no replacement is needed.
                                                                                                                                                                                                                                                                        """
                                                                                                                                                                                                                                                                                return str(self.database_url)
                                                                                                                                                                                                                                                                                
                                                                                                                                                                                                                                                                                    @property
                                                                                                                                                                                                                                                                                        def lavalink_uri(self) -> str:
                                                                                                                                                                                                                                                                                                """Convenience URI for the wavelink node, e.g. ``http://localhost:2333``."""
                                                                                                                                                                                                                                                                                                        scheme = "https" if self.lavalink_https else "http"
                                                                                                                                                                                                                                                                                                                return f"{scheme}://{self.lavalink_host}:{self.lavalink_port}"
                                                                                                                                                                                                                                                                                                                
                                                                                                                                                                                                                                                                                                                
                                                                                                                                                                                                                                                                                                                @lru_cache(maxsize=1)
                                                                                                                                                                                                                                                                                                                def get_settings() -> Settings:
                                                                                                                                                                                                                                                                                                                    """
                                                                                                                                                                                                                                                                                                                        Return the cached ``Settings`` singleton.
                                                                                                                                                                                                                                                                                                                        
                                                                                                                                                                                                                                                                                                                            Using ``lru_cache`` means the ``.env`` file is parsed exactly once,
                                                                                                                                                                                                                                                                                                                                even if ``get_settings()`` is called from multiple modules.
                                                                                                                                                                                                                                                                                                                                    """
                                                                                                                                                                                                                                                                                                                                        return Settings()
                                                                                                                                                                                                                                                                                                                                        
                                                                                                                                                                                                                                                                                                                                        
                                                                                                                                                                                                                                                                                                                                        # Module-level singleton — import this directly in cogs and utilities:
                                                                                                                                                                                                                                                                                                                                        #
                                                                                                                                                                                                                                                                                                                                        #   from config import settings
                                                                                                                                                                                                                                                                                                                                        #
                                                                                                                                                                                                                                                                                                                                        settings: Settings = get_settings()"""