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

from datetime import time
from functools import lru_cache
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
_CALLING_ENGINES = frozenset({"native", "dograh"})
_MAX_ENQUEUE_PAGE_SIZE = 5_000
_DOGRAH_TRIGGER_MODES = frozenset({"test", "production"})
_ANALYSIS_PROVIDERS = frozenset({"mock", "dograh_qa"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# CP11 production secret policy. Lengths are deliberately modest floors, not a
# strength meter: a 32-char token / 12-char password a human cannot guess, and a
# crude distinct-character floor that rejects "aaaa...". Generate real ones with
# `openssl rand -base64 48`.
_MIN_SECRET_LENGTH = 32
_MIN_DISTINCT_SECRET_CHARS = 8
_MIN_PASSWORD_LENGTH = 12


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # CP11: a failed validation (e.g. refusing to boot on a weak production secret)
        # must not print the raw input. Pydantic otherwise appends a truncated dump of
        # every setting -- REDIS_URL / PRIMARY_DB_URL credentials and secrets included --
        # to the startup error, which is exactly what ends up in shipped logs.
        hide_input_in_errors=True,
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
    # CP12-A: a concurrency slot is a lease that expires on its own, so a worker killed
    # mid-dial cannot leak capacity. It must outlast the longest legitimate dial (the
    # Dograh trigger plus reconciliation lookups, each bounded by the Dograh timeouts
    # below) -- validated against those timeouts -- yet stay short enough that a dead
    # worker's slot and its job come back quickly. See docs/CHECKPOINT-12A-NOTES.md.
    concurrency_lease_ttl_seconds: int = 120

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
    # CP10: extra hosts (beyond the Dograh API host) whose private-network
    # addresses the transcript fetch may reach, e.g. a self-hosted object
    # store. Empty by default; loopback/link-local/metadata are never allowed.
    dograh_transcript_extra_hosts: list[str] = []
    # Shared secret we tell Dograh's Webhook node to send back (as a
    # Bearer token or an X-API-Key header, either is accepted) --
    # same "env-configured secret, dev-only-insecure default"
    # convention as telephony_webhook_secret.
    dograh_webhook_secret: str = "dev-only-insecure-dograh-webhook-secret-change-me"
    # CP13: dispositions that mean "this person asked not to be called again". Dograh
    # publishes no fixed code (call dispositions are per-workflow, opt-in); `do_not_call` is
    # the code its own docs suggest as a starter outcome. Exact match only, never a
    # substring. An empty list disables provider-driven suppression.
    dograh_opt_out_dispositions: list[str] = ["do_not_call"]
    # CP13: a provider-supplied Retry-After is untrusted -- never wait longer than this.
    dograh_retry_after_max_seconds: int = 3600
    # CP13 (H1): how many Dograh provider-error failures one contact may accumulate before
    # RecoveryManager stops retrying it. Counted from durable CallAttempt rows, and always in
    # addition to the campaign RetryPolicy.max_retries bound (the lower of the two wins).
    dograh_provider_error_max_retries: int = 2
    # CP13: how many times a retry may be deferred because the Dograh reconciliation lookup
    # itself failed, before the retry is abandoned (no dial) rather than polled forever.
    dograh_reconcile_max_deferrals: int = 5

    # --- Rate limiting (Checkpoint 09 §8.5) ---
    login_rate_limit_per_minute: int = 10
    # CP11: per-principal budgets for authenticated operations, requests/minute.
    # Enqueue and import are the expensive ones (a single enqueue walks up to 100K
    # contacts; an import parses a 5 MB CSV) so they are far tighter than ordinary
    # mutations / analysis reads.
    enqueue_rate_limit_per_minute: int = 5
    # CP12-C: contacts fetched per keyset page when enqueueing a campaign. Bounds the memory
    # and the IN-list of one page; bounded above so a typo cannot materialise a campaign.
    enqueue_page_size: int = 500
    import_rate_limit_per_minute: int = 5
    mutation_rate_limit_per_minute: int = 120
    analysis_rate_limit_per_minute: int = 120

    # CP11: request body caps (bytes), enforced before any parsing/auth work.
    max_request_body_bytes: int = 1_048_576  # 1 MiB: every JSON API, login, webhooks
    max_import_body_bytes: int = 6_291_456  # 6 MiB: CSV import (5 MiB file + multipart overhead)

    # CP11: static backstop for the global outbound kill switch. If true, no new
    # outbound call is admitted regardless of Redis state (survives a Redis flush
    # or restart). Changing it needs a process restart -- the runtime switch is
    # POST/DELETE /api/v1/admin/kill-switch.
    outbound_kill_switch: bool = False

    # CP11: hard cap on a fetched transcript body (decoded bytes) and on how many
    # lines of it are stored. The URL is supplied by the webhook body, so the
    # response size is attacker-influenced.
    dograh_transcript_max_bytes: int = 2_000_000
    dograh_transcript_max_lines: int = 5_000
    webhook_rate_limit_per_minute: int = 120

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

    # --- CP14B: post-call intelligence reliability + Dograh QA adapter ---
    # Provider: "mock" (CP06 fake) or "dograh_qa" (reads the QA node's annotations
    # from Dograh's GET run endpoint; it makes NO LLM request of its own).
    # Lease/fencing: a claimed analysis is owned for this long; an expired lease is
    # recoverable by any worker. Must exceed the longest Dograh fetch (validated below).
    analysis_lease_seconds: int = 120
    # Retry policy -- the WORKER is the only retry owner (no nested retry loops).
    # delay = min(max, base * 2**(attempt-1)) * uniform(0.5, 1.0); Retry-After wins if larger.
    analysis_retry_base_seconds: int = 30
    analysis_retry_max_seconds: int = 900
    # "QA result not ready" is polled (without consuming attempts) until this deadline,
    # measured from the analysis row's creation; then the analysis is SKIPPED as
    # "QA never produced a result".
    analysis_qa_ready_deadline_seconds: int = 1800
    analysis_initial_delay_seconds: int = 0
    # Sweeper (runs inside each analysis worker; idempotent and safe to run concurrently).
    analysis_sweeper_interval_seconds: int = 15
    analysis_sweeper_batch_size: int = 100
    # A PENDING/RETRY_WAIT row whose publication is older than this is republished
    # (recovers jobs lost to a Redis restart or a failed post-commit publish).
    analysis_republish_after_seconds: int = 300
    # Registers completed calls that have no analysis row, but only this recent -- a
    # bounded safety net, deliberately NOT a historical backfill.
    analysis_sweeper_lookback_hours: int = 72
    # Mirrors the QA node's "Minimum Call Duration" in Dograh (default 15s). Calls shorter
    # than this are SKIPPED without a fetch. MUST match the Dograh node's setting.
    dograh_qa_min_duration_seconds: float = 15.0
    # Optional: the exact key under run.annotations where the QA node's JSON lands. Its
    # naming is UNDOCUMENTED; when empty the adapter finds the single entry that carries our
    # contract marker (schema_version == "cp14b.v1") and refuses ambiguity.
    dograh_qa_annotation_key: str = ""
    # Fallback for webhooks whose payload_template predates `workflow_id`.
    dograh_workflow_id: int | None = None
    # Input bounds (the transcript is validated/measured here even though Dograh, not this
    # backend, sends it to its QA LLM) and the maximum accepted annotation size.
    analysis_max_message_chars: int = 2_000
    analysis_max_transcript_chars: int = 60_000
    analysis_max_output_bytes: int = 16_384
    # Daily ESTIMATED spend for post-call analysis -- a control SEPARATE from CP14's dialing
    # cap. Both must be set together; when unset (or the provider is not "mock") new analysis
    # work FAILS CLOSED: it stays pending with reason budget_not_configured. All figures are
    # estimates, never billing. Counted against the BUDGET_TIMEZONE day.
    analysis_daily_estimated_spend_cap: float | None = None
    analysis_estimated_cost_per_analysis: float | None = None
    analysis_budget_recheck_seconds: int = 600

    # --- Compliance basics + daily spend cap (Checkpoint 14) ---
    # Single-tenant: one default region / timezone for the whole system; a campaign may
    # override both at creation. Numbers written without a country code are read in the
    # campaign's region; dialing is only allowed to ALLOWED_DIAL_REGIONS (JSON list in env).
    default_region: str = "IN"
    default_timezone: str = "Asia/Kolkata"
    allowed_dial_regions: list[str] = ["IN"]

    # Compliance backstop: no campaign window may extend beyond this, in the campaign's own
    # timezone. NOT legal advice -- a conservative default pending the owner's legal review.
    hard_calling_window_start: time = time(9, 0)
    hard_calling_window_end: time = time(21, 0)
    # Defaults for a campaign's RetryPolicy (a campaign created without one, and legacy
    # campaigns that have no row). PRODUCT DECISIONS -- see docs/CHECKPOINT-14-NOTES.md.
    default_calling_window_start: time = time(9, 0)
    default_calling_window_end: time = time(21, 0)
    default_max_retries: int = 2
    default_retry_spacing_seconds: list[int] = [3600, 14400]
    max_retries_ceiling: int = 5
    min_retry_backoff_seconds: int = 300
    max_retry_backoff_seconds: int = 604_800  # 7 days

    # Daily spend cap. All figures are ESTIMATES, not billing: the provider-side limit in
    # Dograh remains the real backstop. 0 blocks all dialing. The day is BUDGET_TIMEZONE's.
    daily_dial_cap: int = 1000
    daily_estimated_spend_cap: float | None = None
    estimated_cost_per_minute: float | None = None
    # Attempts with no recorded duration are counted as this many minutes.
    estimated_minutes_per_unknown_attempt: float = 1.0
    budget_timezone: str = "Asia/Kolkata"
    # In-process cache of the budget check: overshoot is bounded by this TTL plus the
    # number of dials in flight at once (see docs/CHECKPOINT-14-NOTES.md).
    budget_check_cache_ttl_seconds: float = 2.0

    # --- Provider/voice config validation (Checkpoint 10) ---
    # Every value below is a closed set whose typo would otherwise fail
    # OPEN: an unknown ENVIRONMENT skips the production checks entirely, an
    # unknown CALLING_ENGINE silently runs the native/mock engine, and an
    # unknown DOGRAH_TRIGGER_MODE silently hits Dograh's /test/ endpoint.
    @model_validator(mode="after")
    def _validate_cp14_config(self) -> "Settings":
        problems: list[str] = []

        # A timezone that cannot load (missing tzdata, typo) must stop the boot: falling
        # back to a fixed UTC offset would silently move every calling window.
        for name in ("default_timezone", "budget_timezone"):
            try:
                ZoneInfo(getattr(self, name))
            except (ZoneInfoNotFoundError, ValueError, OSError):
                problems.append(f"{name.upper()} is not a loadable IANA timezone")

        supported = phonenumbers.SUPPORTED_REGIONS
        self.default_region = self.default_region.upper()
        self.allowed_dial_regions = [r.upper() for r in self.allowed_dial_regions]
        if self.default_region not in supported:
            problems.append("DEFAULT_REGION must be a valid ISO 3166-1 alpha-2 region code")
        if not self.allowed_dial_regions or any(
            r not in supported for r in self.allowed_dial_regions
        ):
            problems.append("ALLOWED_DIAL_REGIONS must be a non-empty list of valid region codes")
        elif self.default_region not in self.allowed_dial_regions:
            problems.append("DEFAULT_REGION must be one of ALLOWED_DIAL_REGIONS")

        if self.hard_calling_window_start >= self.hard_calling_window_end:
            problems.append("HARD_CALLING_WINDOW_START must be before HARD_CALLING_WINDOW_END")
        if self.default_calling_window_start == self.default_calling_window_end:
            problems.append("DEFAULT_CALLING_WINDOW_START and _END must differ")

        if not 0 <= self.default_max_retries <= self.max_retries_ceiling:
            problems.append("DEFAULT_MAX_RETRIES must be between 0 and MAX_RETRIES_CEILING")
        if len(self.default_retry_spacing_seconds) != self.default_max_retries:
            problems.append("DEFAULT_RETRY_SPACING_SECONDS must have DEFAULT_MAX_RETRIES entries")
        if any(
            not self.min_retry_backoff_seconds <= s <= self.max_retry_backoff_seconds
            for s in self.default_retry_spacing_seconds
        ):
            problems.append(
                "DEFAULT_RETRY_SPACING_SECONDS entries must lie within "
                "MIN_RETRY_BACKOFF_SECONDS..MAX_RETRY_BACKOFF_SECONDS"
            )

        if self.daily_dial_cap < 0:
            problems.append("DAILY_DIAL_CAP must be >= 0")
        for name in (
            "daily_estimated_spend_cap",
            "estimated_cost_per_minute",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                problems.append(f"{name.upper()} must be >= 0")
        if (self.daily_estimated_spend_cap is None) != (self.estimated_cost_per_minute is None):
            problems.append(
                "DAILY_ESTIMATED_SPEND_CAP and ESTIMATED_COST_PER_MINUTE must be set together"
            )
        if self.estimated_minutes_per_unknown_attempt < 0:
            problems.append("ESTIMATED_MINUTES_PER_UNKNOWN_ATTEMPT must be >= 0")
        if self.budget_check_cache_ttl_seconds < 0:
            problems.append("BUDGET_CHECK_CACHE_TTL_SECONDS must be >= 0")

        # --- CP14B analysis settings ---
        if self.analysis_llm_provider not in _ANALYSIS_PROVIDERS:
            problems.append(f"ANALYSIS_LLM_PROVIDER must be one of {sorted(_ANALYSIS_PROVIDERS)}")
        for name in (
            "analysis_lease_seconds",
            "analysis_retry_base_seconds",
            "analysis_retry_max_seconds",
            "analysis_qa_ready_deadline_seconds",
            "analysis_sweeper_interval_seconds",
            "analysis_sweeper_batch_size",
            "analysis_republish_after_seconds",
            "analysis_sweeper_lookback_hours",
            "analysis_max_message_chars",
            "analysis_max_transcript_chars",
            "analysis_max_output_bytes",
            "analysis_budget_recheck_seconds",
            "analysis_max_attempts",
        ):
            if getattr(self, name) <= 0:
                problems.append(f"{name.upper()} must be > 0")
        if self.analysis_initial_delay_seconds < 0:
            problems.append("ANALYSIS_INITIAL_DELAY_SECONDS must be >= 0")
        if self.analysis_retry_max_seconds < self.analysis_retry_base_seconds:
            problems.append("ANALYSIS_RETRY_MAX_SECONDS must be >= ANALYSIS_RETRY_BASE_SECONDS")
        if self.dograh_qa_min_duration_seconds < 0:
            problems.append("DOGRAH_QA_MIN_DURATION_SECONDS must be >= 0")
        # A lease shorter than one Dograh fetch (connect + read, with headroom) would let a
        # healthy worker lose its claim mid-request.
        longest_fetch = 2 * (self.dograh_connect_timeout_seconds + self.dograh_read_timeout_seconds)
        if self.analysis_lease_seconds <= longest_fetch:
            problems.append(
                "ANALYSIS_LEASE_SECONDS must exceed 2 x (DOGRAH_CONNECT_TIMEOUT_SECONDS + "
                f"DOGRAH_READ_TIMEOUT_SECONDS) = {longest_fetch:g}s"
            )
        for name in ("analysis_daily_estimated_spend_cap", "analysis_estimated_cost_per_analysis"):
            value = getattr(self, name)
            if value is not None and value < 0:
                problems.append(f"{name.upper()} must be >= 0")
        if (self.analysis_daily_estimated_spend_cap is None) != (
            self.analysis_estimated_cost_per_analysis is None
        ):
            problems.append(
                "ANALYSIS_DAILY_ESTIMATED_SPEND_CAP and ANALYSIS_ESTIMATED_COST_PER_ANALYSIS "
                "must be set together"
            )

        if problems:
            raise ValueError(
                "Invalid compliance/spend configuration:\n  - " + "\n  - ".join(problems)
            )
        return self


    @model_validator(mode="after")
    def _validate_provider_config(self) -> "Settings":
        problems: list[str] = []

        if self.environment not in _ENVIRONMENTS:
            problems.append(f"ENVIRONMENT must be one of {sorted(_ENVIRONMENTS)}")
        if self.calling_engine not in _CALLING_ENGINES:
            problems.append(f"CALLING_ENGINE must be one of {sorted(_CALLING_ENGINES)}")
        if self.dograh_trigger_mode not in _DOGRAH_TRIGGER_MODES:
            problems.append(f"DOGRAH_TRIGGER_MODE must be one of {sorted(_DOGRAH_TRIGGER_MODES)}")
        if self.dograh_connect_timeout_seconds <= 0 or self.dograh_read_timeout_seconds <= 0:
            problems.append(
                "DOGRAH_CONNECT_TIMEOUT_SECONDS and DOGRAH_READ_TIMEOUT_SECONDS must be > 0"
            )

        if self.dograh_retry_after_max_seconds <= 0:
            problems.append("DOGRAH_RETRY_AFTER_MAX_SECONDS must be > 0")
        if self.dograh_provider_error_max_retries < 0:
            problems.append("DOGRAH_PROVIDER_ERROR_MAX_RETRIES must be >= 0")
        if self.dograh_reconcile_max_deferrals < 1:
            problems.append("DOGRAH_RECONCILE_MAX_DEFERRALS must be >= 1")
        if any(not code.strip() for code in self.dograh_opt_out_dispositions):
            problems.append("DOGRAH_OPT_OUT_DISPOSITIONS must not contain empty codes")

        # CP12-A: a lease that can expire while its dial is still running would let a second
        # worker take the same slot. Three sequential Dograh requests (trigger + two
        # reconciliation lookups) is the longest path in the dial flow.
        longest_dial = 3 * (self.dograh_connect_timeout_seconds + self.dograh_read_timeout_seconds)
        if self.concurrency_lease_ttl_seconds <= longest_dial:
            problems.append(
                "CONCURRENCY_LEASE_TTL_SECONDS must exceed 3 x (DOGRAH_CONNECT_TIMEOUT_SECONDS "
                f"+ DOGRAH_READ_TIMEOUT_SECONDS) = {longest_dial:g}s"
            )

        # CP11: a zero/negative cap would make a safety limit reject (or divide) everything.
        for name in (
            "enqueue_rate_limit_per_minute",
            "import_rate_limit_per_minute",
            "mutation_rate_limit_per_minute",
            "analysis_rate_limit_per_minute",
            "max_request_body_bytes",
            "max_import_body_bytes",
            "dograh_transcript_max_bytes",
            "dograh_transcript_max_lines",
        ):
            if getattr(self, name) <= 0:
                problems.append(f"{name.upper()} must be > 0")

        if not 1 <= self.enqueue_page_size <= _MAX_ENQUEUE_PAGE_SIZE:
            problems.append(f"ENQUEUE_PAGE_SIZE must be between 1 and {_MAX_ENQUEUE_PAGE_SIZE}")

        if self.calling_engine == "dograh":
            parsed = urlparse(self.dograh_api_base_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                problems.append("DOGRAH_API_BASE_URL must be an http(s) URL with a host")
            if self.environment == "staging":
                if not self.dograh_api_key:
                    problems.append("DOGRAH_API_KEY is required when CALLING_ENGINE=dograh")
                if not self.dograh_trigger_uuid:
                    problems.append("DOGRAH_TRIGGER_UUID is required when CALLING_ENGINE=dograh")
            if self.environment == "production" and (
                parsed.scheme != "https" or (parsed.hostname or "") in _LOCAL_HOSTS
            ):
                problems.append(
                    "DOGRAH_API_BASE_URL must be an https URL to a non-local host in production "
                    "(the API key is sent on every request)"
                )

        if problems:
            raise ValueError(
                "Invalid provider/voice configuration:\n  - " + "\n  - ".join(problems)
            )
        return self

    # --- Production config validation (Checkpoint 09 §10, hardened in CP11) ---
    # "Do not silently use development defaults in production." Fails closed: the
    # process refuses to boot rather than substitute a default. Error messages name
    # the offending FIELD only -- never a secret value.
    #
    # * staging AND production: no dev-only default secret. A shared, network-reachable
    #   staging box running the published default JWT key lets anyone forge admin tokens.
    # * production only: minimum secret strength, all secrets distinct, distinct
    #   admin/operator identities, no wildcard CORS, real (non-mock) engine.
    @model_validator(mode="after")
    def _validate_production_config(self) -> "Settings":
        if self.environment not in ("staging", "production"):
            return self
        production = self.environment == "production"

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

        if production:
            self._check_production_secrets(problems)

            if self.admin_username.strip() == "" or self.operator_username.strip() == "":
                problems.append("ADMIN_USERNAME and OPERATOR_USERNAME must not be empty")
            elif self.admin_username == self.operator_username:
                problems.append(
                    "ADMIN_USERNAME and OPERATOR_USERNAME must differ "
                    "(audit events and rate limits are attributed by username)"
                )

            if any(origin.strip() == "*" for origin in self.admin_cors_origins):
                problems.append("ADMIN_CORS_ORIGINS must list explicit origins, not '*'")

            # CP10: every native provider is still a mock, so production on the
            # native engine would "place" fake calls. Never a silent fallback.
            if self.calling_engine != "dograh":
                problems.append(
                    "CALLING_ENGINE must be 'dograh' when ENVIRONMENT=production "
                    "(the native engine only has mock providers)"
                )

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
                f"Refusing to start with ENVIRONMENT={self.environment} and insecure/incomplete "
                "configuration:\n  - " + "\n  - ".join(problems)
            )
        return self

    def _check_production_secrets(self, problems: list[str]) -> None:
        machine_secrets = {
            "jwt_signing_key": self.jwt_signing_key,
            "telephony_webhook_secret": self.telephony_webhook_secret,
            "dograh_webhook_secret": self.dograh_webhook_secret,
        }
        passwords = {
            "admin_password": self.admin_password,
            "operator_password": self.operator_password,
        }
        for name, value in machine_secrets.items():
            if len(value) < _MIN_SECRET_LENGTH:
                problems.append(f"{name.upper()} must be at least {_MIN_SECRET_LENGTH} characters")
            elif len(set(value)) < _MIN_DISTINCT_SECRET_CHARS:
                problems.append(f"{name.upper()} is too repetitive to be a real secret")
        for name, value in passwords.items():
            if len(value) < _MIN_PASSWORD_LENGTH:
                problems.append(
                    f"{name.upper()} must be at least {_MIN_PASSWORD_LENGTH} characters"
                )
        for name, value in {**machine_secrets, **passwords}.items():
            if value != value.strip():
                problems.append(f"{name.upper()} must not start or end with whitespace")

        # One leaked secret must not unlock another: all must differ from each other
        # (and the Dograh API key, which is also shared with a third party).
        named = {**machine_secrets, **passwords}
        if self.dograh_api_key:
            named["dograh_api_key"] = self.dograh_api_key
        seen: dict[str, str] = {}
        for name, value in named.items():
            if value in seen:
                problems.append(f"{name.upper()} must differ from {seen[value].upper()}")
            else:
                seen[value] = name


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance -- environment is read once per process."""
    return Settings()
