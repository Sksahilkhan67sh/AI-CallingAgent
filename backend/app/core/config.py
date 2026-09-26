"""
Application configuration.

Reads infrastructure configuration from environment variables, per
Deployment/Environment-Config.md. Business configuration (retry policy,
agent persona/script, etc.) is NOT read here -- it lives in the database
(`retry_policy`, `agent_config` tables) and is set at runtime via the
admin dashboard, not via deployment.

Every setting has a safe, non-functional default so the service can boot
in a local/dev environment without any secrets configured. Production
deployments must override these via real environment variables / a
secrets manager -- never via committed files.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service metadata ---
    app_name: str = "AI Calling Agent API"
    environment: str = "development"
    log_level: str = "debug"

    # --- API / auth (Environment-Config.md §2.4) ---
    jwt_signing_key: str = "dev-only-insecure-key-change-me"
    jwt_expiry_seconds: int = 3600
    api_rate_limit_per_minute: int = 60

    # --- Admin dashboard auth (Checkpoint 07) ---
    # No user table/registration flow -- two fixed operator identities,
    # same "env-configured secret, dev-only-insecure default" convention
    # already used for telephony_webhook_secret. JWTs are signed with
    # the jwt_signing_key setting above, which existed but was unused
    # until this checkpoint.
    admin_username: str = "admin"
    admin_password: str = "dev-only-insecure-admin-password-change-me"
    operator_username: str = "operator"
    operator_password: str = "dev-only-insecure-operator-password-change-me"
    # Origins the browser-based admin dashboard is served from.
    admin_cors_origins: list[str] = ["http://localhost:3000"]
    # Best-effort liveness signal for the system health page (§25) --
    # each worker loop touches its own key; a missing/expired key means
    # "no worker has run a loop iteration within this window", not a
    # guaranteed crash (see docs/CHECKPOINT-07-NOTES.md).
    worker_heartbeat_ttl_seconds: int = 30

    # --- Database & data layer (Environment-Config.md §2.1) ---
    primary_db_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/ai_calling_agent"
    )
    primary_db_pool_size: int = 5

    # --- Queue / dialer (Checkpoint 03) ---
    redis_url: str = "redis://localhost:6379/0"
    queue_stream_key: str = "calls:outbound"
    queue_consumer_group: str = "dialer-workers"
    # How long a claimed-but-unacked stream entry may sit idle before
    # another worker is allowed to reclaim it (a crashed worker's job).
    queue_reclaim_idle_ms: int = 30_000

    # Telephony provider selection -- "mock" is the only supported value
    # until real provider credentials exist (see
    # docs/CHECKPOINT-03-NOTES.md).
    telephony_provider: str = "mock"
    telephony_webhook_secret: str = "dev-only-insecure-webhook-secret-change-me"

    # CPS (calls per second) and concurrency limits. Applied globally and
    # independently per campaign / per provider via separate counters --
    # see docs/CHECKPOINT-03-NOTES.md for why these are config, not a DB
    # column.
    global_cps_limit: int = 20
    campaign_cps_limit: int = 5
    provider_cps_limit: int = 20
    global_concurrency_limit: int = 100
    campaign_concurrency_limit: int = 30
    provider_concurrency_limit: int = 100

    # Circuit breaker (per provider)
    circuit_breaker_error_threshold: int = 5
    circuit_breaker_open_seconds: int = 30

    # --- Real-time AI conversation (Checkpoint 04) ---
    # "mock" is the only supported value for each until real provider
    # credentials exist -- see docs/CHECKPOINT-04-NOTES.md.
    stt_provider: str = "mock"
    llm_provider: str = "mock"
    tts_provider: str = "mock"
    audio_gateway_provider: str = "mock"

    # --- Post-call intelligence (Checkpoint 06) ---
    # "mock" is the only supported value until real credentials exist,
    # same convention as the CP04 providers above.
    analysis_llm_provider: str = "mock"
    analysis_stream_key: str = "analysis:jobs"
    analysis_consumer_group: str = "analysis-workers"
    # Idle time before a crashed analysis worker's unacked job is
    # reclaimed by another worker (mirrors queue_reclaim_idle_ms), and
    # also the effective backoff window between bounded retry attempts
    # for a transiently-failed analysis (Checkpoint 06 §21-22).
    analysis_reclaim_idle_ms: int = 60_000
    analysis_max_attempts: int = 3
    analysis_prompt_version: str = "POST_CALL_ANALYSIS_V1"
    analysis_version: str = "v1"
    # Cost control (§30): bound how much transcript is sent to the LLM.
    # Truncation preserves the beginning, the ending, and a sample of
    # the middle -- see app/services/analysis/transcript.py.
    analysis_max_transcript_messages: int = 200


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance -- environment is read once per process."""
    return Settings()
