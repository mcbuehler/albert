"""App configuration: business rules and external services in one place.

Secrets and per-environment values stay in environment variables; read them through env().
"""

import os
from datetime import time

# --- Scheduled callbacks ---------------------------------------------------
CALLING_START = time(9, 0)      # customer-local
CALLING_END = time(22, 0)       # customer-local, exclusive
MAX_PENDING_PER_CUSTOMER = 3
MAX_ATTEMPTS = 4                # 1 call + 3 retries
RETRY_MINUTES = 60
MEMORY_RETRY_MINUTES = 5        # memory couldn't be loaded: retry soon, and it doesn't count as an attempt
# Attempts whose Vapi call id was never saved (dispatcher died or the DB write failed right after dialling):
# after this many minutes the dispatcher looks the call up in Vapi's call list instead of guessing.
UNRECORDED_ATTEMPT_MINUTES = 2
UNDIALLED_RETRY_MINUTES = 5     # such an attempt had no call in Vapi: nothing was dialled, retry soon, not counted
VAPI_CALL_MATCH_BEFORE_MINUTES = 1  # Vapi calls created in [attempt start - before, attempt start + after]
VAPI_CALL_MATCH_AFTER_MINUTES = 5
STALE_CALL_MINUTES = 120        # safety net: an attempt Vapi never reports as ended counts as ended after this
MAX_DISPATCH_PER_RUN = 5        # cron run: at most this many calls (each on a different, idle phone number)

# Call archive cron (/api/cron/archive). False turns capture off: the route then writes nothing (503).
ARCHIVE_CAPTURE_ENABLED = True
ARCHIVE_LOCK_ID = 82470215
ARCHIVE_SCAN_LEASE_MINUTES = 2
ARCHIVE_INITIAL_LOOKBACK_MINUTES = 10
ARCHIVE_SCAN_OVERLAP_MINUTES = 2
ARCHIVE_LIST_LIMIT = 100        # smaller pages bound SQL writes within one invocation
ARCHIVE_MAX_LIST_REQUESTS = 6
ARCHIVE_MAX_REFRESH_CALLS = 5
ARCHIVE_RUN_SECONDS = 45
ARCHIVE_REFRESH_SECONDS = 60
ARCHIVE_PROVIDER_RETENTION_DAYS = 14  # Vapi keeps calls this long (account setting); reconcile sweeps this period
ARCHIVE_RECONCILE_WINDOW_HOURS = 6
ARCHIVE_MAX_RECONCILE_REQUESTS = 2    # per run, after recent discovery; a first sweep doubles as the backfill
ARCHIVE_AUDIO_BUCKET = "call-recordings"            # private Supabase Storage bucket, created by staff
ARCHIVE_AUDIO_MAX_BYTES = 100 * 1024 * 1024         # larger recordings fail instead of exhausting the function
ARCHIVE_AUDIO_SPOOL_BYTES = 8 * 1024 * 1024         # downloads above this spill from memory to temporary disk
ARCHIVE_MAX_AUDIO_PER_RUN = 3
ARCHIVE_MAX_CLEANUP_PER_RUN = 10      # recordings of deleted calls removed from storage per run
ARCHIVE_AUDIO_MIN_SECONDS = 15        # start a recording only with at least this much of the run budget left
ARCHIVE_AUDIO_CLAIM_MINUTES = 5       # a crashed run's recording is taken over after this
ARCHIVE_AUDIO_RETRY_MAX_MINUTES = 60  # retries wait 1, 2, 4 … minutes, at most this long
ARCHIVE_AUDIO_GIVE_UP_HOURS = 24      # then the recording counts as unavailable
ARCHIVE_TRANSCRIPT_WAIT_MINUTES = 10  # an ended call still without transcript after this is "empty"
ARCHIVE_PLAYBACK_SECONDS = 300       # lifetime of a staff playback link (Supabase signed URL)

# Vapi endedReasons that mean the customer was reached and the callback is done.
# Everything else (no answer, busy, voicemail, silence, provider/pipeline errors, unknown) is retried.
ANSWERED_REASONS = frozenset({
    "customer-ended-call",
    "assistant-ended-call",
    "assistant-said-end-call-phrase",
    "assistant-ended-call-after-message-spoken",
    "assistant-ended-call-with-hangup-task",
    "assistant-forwarded-call",
    "exceeded-max-duration",
})
# Vapi endedReasons where nobody spoke with Albert (voicemail detection, no answer): the
# end-of-call-report isn't stored in memory. The callback is retried like any other unanswered call.
NOT_REACHED_REASONS = frozenset({"voicemail", "customer-did-not-answer"})

# --- Calls list (/api/calls) -----------------------------------------------
CALL_STATUSES = ("scheduled", "calling", "completed", "no_answer", "voicemail", "failed", "cancelled")
CALL_PAST_STATUSES = ("completed", "no_answer", "voicemail", "failed")  # the "past" filter
CALL_NO_ANSWER_REASONS = ("customer-did-not-answer", "customer-busy")  # Vapi endedReasons shown as "No answer"
CALL_LIST_DEFAULT_LIMIT = 50
CALL_LIST_MAX_LIMIT = 100
CALL_NOTE_MAX_CHARS = 4000         # matches the call_notes check in migration 014
CALL_NOTE_AUTHOR_MAX_CHARS = 320   # author label: name from the login, else its email

# --- Customers and assistant prompt ----------------------------------------
DEFAULT_TIMEZONE = "Europe/Zurich"  # used when the caller isn't a known customer
UNKNOWN_CUSTOMER_NAME = "unknown"
MEMORY_VARIABLE = "previous_messages"  # {{previous_messages}} in the assistant prompt (inbound and outbound)
NO_MEMORY_TEXT = "No previous conversations with this customer."
INSTRUCTIONS_VARIABLE = "call_instructions"  # {{call_instructions}}: staff or customer instructions for this call
NO_INSTRUCTIONS_TEXT = "No special instructions for this call."
MAX_INSTRUCTIONS_LENGTH = 1000
CUSTOMER_INSTRUCTIONS_PREFIX = "Requested by the customer: "  # AI-scheduled instructions (e.g. a reminder)
PENDING_CALLBACKS_VARIABLE = "pending_callbacks"  # {{pending_callbacks}}: the customer's pending callbacks
NO_PENDING_CALLBACKS_TEXT = "No pending callbacks."
PENDING_CALLBACKS_UNAVAILABLE_TEXT = "Pending callbacks could not be loaded right now."

# --- Inbound sign-ups (Vapi tool onboard_customer) -------------------------------------------------
# Unknown callers are only answered from these countries (phone region -> env suffix). They get the assistant in
# env VAPI_INBOUND_ASSISTANT_ID_<suffix> (fallback VAPI_ASSISTANT_ID); if they sign up, VAPI_ASSISTANT_ID_<suffix>
# and VAPI_PHONE_NUMBER_ID_<suffix> become their defaults.
ONBOARDING_REGIONS = {"US": "US", "CA": "US", "CH": "CH"}
# Time zone guessed from the number (area code); used when the number maps to no single zone.
REGION_DEFAULT_TIMEZONES = {"US": "America/New_York", "CA": "America/Toronto", "CH": "Europe/Zurich"}
MAX_INBOUND_SIGNUPS_PER_DAY = 20  # abuse guard: more sign-ups in 24 h are refused (the team is notified instead)
INBOUND_COUNTRY_NOT_SUPPORTED_MESSAGE = ("Sorry, Albert is only available in the United States, Canada and "
                                         "Switzerland for now. Thank you for calling!")

# --- Team notifications (Vapi tool notify_team, emailed to env TEAM_EMAIL) ----------------------------
NOTIFICATION_REASONS = {  # reason -> email subject label
    "no_consent": "Caller doesn't want to be recorded",
    "institution_lead": "Institution lead",
    "caregiver_lead": "Caregiver lead",
    "inbound_signup": "New customer signed up on a call (please review)",
    "request": "Request for the team",
    "other": "Note",
}
MAX_NOTIFICATION_NAME_LENGTH = 200     # contact_name, organization
MAX_NOTIFICATION_SUMMARY_LENGTH = 1000
MAX_NOTIFICATION_EMAILS_PER_RUN = 10   # emailed by the dispatcher cron, not during the call
NOTIFICATION_EMAIL_MAX_AGE_HOURS = 24  # failed emails are retried every run for this long (the row stays)
NOTIFICATION_EMAIL_CLAIM_MINUTES = 10   # retry a claimed row after a crashed/interrupted dispatcher run

# --- External services -----------------------------------------------------
SUPABASE_JWKS_PATH = "/auth/v1/.well-known/jwks.json"  # Supabase Auth public signing keys
SUPABASE_JWT_AUDIENCE = "authenticated"  # `aud` of tokens for logged-in users
VAPI_API_URL = "https://api.vapi.ai"
VAPI_CALL_URL = f"{VAPI_API_URL}/call"
HINDSIGHT_API_URL = "https://api.hindsight.vectorize.io"
# Hard deadlines for loading a customer's memory. Calls never run without memory:
# inbound must answer Vapi's assistant-request within ~7.5 s, otherwise the call is rejected;
# outbound simply isn't dialled (the callback is retried).
INBOUND_MEMORY_TIMEOUT_SECONDS = 5.0
OUTBOUND_MEMORY_TIMEOUT_SECONDS = 20.0
ONBOARDING_MEMORY_TIMEOUT_SECONDS = 20.0  # creating a new customer's Hindsight bank
INBOUND_SIGNUP_MEMORY_TIMEOUT_SECONDS = 12.0  # the same during a call (tool onboard_customer; Vapi waits 30 s)
DANGER_MEMORY_TIMEOUT_SECONDS = 90.0  # deleting / erasing a customer's Hindsight bank (danger zone; slow for big banks)
INBOUND_NO_NUMBER_MESSAGE = "Sorry, we can't take calls from hidden numbers. Please call again with your number shown."
INBOUND_NO_MEMORY_MESSAGE = "Sorry, we can't take your call right now. Please try again in a few minutes."
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/llm/context"
HEALTHCHECK_TIMEOUT_SECONDS = 5  # dispatcher ping to Healthchecks.io (env HEALTHCHECKS_PING_URL)
SMTP_HOST = "smtp.gmail.com"  # Google Workspace, using SMTP_USER and an SMTP_PASSWORD app password
SMTP_PORT = 587               # STARTTLS
SMTP_TIMEOUT_SECONDS = 5      # runs inside a Vapi tool request


# Env vars the app can't work without (checked by /api/health; optional ones aren't listed).
REQUIRED_ENV = ("DATABASE_URL", "REL8_API_AUTH_TOKEN", "CRON_SECRET", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
                "VAPI_API_KEY", "VAPI_ASSISTANT_ID", "HINDSIGHT_API_KEY", "BRAVE_API_KEY", "SUPABASE_SECRET_KEY")


def env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable when needed (not at import), so Vercel and tests can set it later.

    Used for secrets and per-environment values: DATABASE_URL, REL8_API_AUTH_TOKEN, CRON_SECRET,
    SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY (or the legacy anon key; public, used by the login page),
    SUPABASE_JWT_SECRET (only for legacy HS256 projects), SUPABASE_SECRET_KEY (call recordings
    in Supabase Storage), VAPI_API_KEY, VAPI_ASSISTANT_ID,
    HINDSIGHT_API_KEY, BRAVE_API_KEY, VAPI_INBOUND_ASSISTANT_ID_US / _CH,
    VAPI_ASSISTANT_ID_US / _CH, VAPI_PHONE_NUMBER_ID_US / _CH,
    HEALTHCHECKS_PING_URL, SMTP_USER, SMTP_PASSWORD, TEAM_EMAIL,
    LOCAL_LOGGING, LOG_STDOUT.
    """
    return os.getenv(name, default)
