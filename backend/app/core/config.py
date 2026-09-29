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

from pydantic import model_validator
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

    # --- Dograh integration (Checkpoint 08) ---
    # calling_engine="native" keeps the existing Checkpoint 03/04 path
    # (our own TelephonyProvider + StreamingSTT/LLM/TTS/AudioGateway,
    # all still "mock" until real credentials exist -- see above).
    # calling_engine="dograh" delegates telephony, STT, LLM, TTS, and
    # the post-call webhook entirely to a self-hosted Dograh instance;
    # see docs/CHECKPOINT-08-NOTES.md for what that instance itself
    # still needs (a published workflow with an API Trigger node and a
    # Webhook node, and its own telephony/model credentials -- none of
    # that lives in this repository).
    calling_engine: str = "native"
    dograh_api_base_url: str = "http://localhost:8000"
    dograh_api_key: str = ""
    # The API Trigger node's UUID from the Dograh workflow (Settings ->
    # the trigger node's dialog). Required when calling_engine="dograh".
    dograh_trigger_uuid: str = ""
    # "test" runs the workflow's latest draft; "production" requires
    # the workflow to be published first. See Dograh's own API Trigger
    # docs -- this is Dograh's distinction, not one we invented.
    dograh_trigger_mode: str = "test"
    # §1.2: bounded, independently-configurable connect vs. read
    # timeouts -- see app/services/telephony/dograh_client.py.
    dograh_connect_timeout_seconds: float = 5.0
    dograh_read_timeout_seconds: float = 15.0
    # Shared secret we tell Dograh's Webhook node to send back (as a
    # Bearer token or an X-API-Key header, either is accepted) --
    # same "env-configured secret, dev-only-insecure default"
    # convention as telephony_webhook_secret.
    dograh_webhook_secret: str = "dev-only-insecure-dograh-webhook-secret-change-me"

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

    # --- Production config validation (Checkpoint 09 §10) ---
    # "Do not silently use development defaults in production." Every
    # value checked here is a dev-only-insecure default that already
    # exists elsewhere in this file (jwt_signing_key, the admin/operator
    # passwords, telephony_webhook_secret, dograh_webhook_secret) --
    # this validator doesn't invent new secrets, it just refuses to
    # boot with the known-insecure ones once environment="production".
    @model_validator(mode="after")
    def _validate_production_config(self) -> "Settings":
        if self.environment != "production":
            return self

        problems: list[str] = []

        _DEV_DEFAULTS = {
            "jwt_signing_key": "dev-only-insecure-key-change-me",
            "admin_password": "dev-only-insecure-admin-password-change-me",
            "operator_password": "dev-only-insecure-operator-password-change-me",
            "telephony_webhook_secret": "dev-only-insecure-webhook-secret-change-me",
            "dograh_webhook_secret": "dev-only-insecure-dograh-webhook-secret-change-me",
        }
        for field_name, insecure_default in _DEV_DEFAULTS.items():
            if getattr(self, field_name) == insecure_default:
                problems.append(f"{field_name.upper()} is still set to its dev-only default")

        if self.calling_engine == "dograh":
            if not self.dograh_api_key:
                problems.append("DOGRAH_API_KEY is required when CALLING_ENGINE=dograh")
            if not self.dograh_trigger_uuid:
                problems.append("DOGRAH_TRIGGER_UUID is required when CALLING_ENGINE=dograh")
            if self.dograh_trigger_mode != "production":
                problems.append(
                    "DOGRAH_TRIGGER_MODE must be 'production' (not 'test') when "
                    "ENVIRONMENT=production -- see docs/CHECKPOINT-09-NOTES.md §1.1"
                )

        if problems:
            raise ValueError(
                "Refusing to start with ENVIRONMENT=production and insecure/incomplete "
                "configuration:\n  - " + "\n  - ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance -- environment is read once per process."""
    return Settings()
