# Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Supabase Postgres via the **transaction pooler** (port 6543, `?sslmode=require`, URL-encoded password) |
| `REL8_API_AUTH_TOKEN` | yes | Bearer token Vapi sends to the webhook and tool endpoints |
| `CRON_SECRET` | yes | Bearer token Vercel Cron sends to the dispatcher and the call archive |
| `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | yes | Staff login (the publishable key is public by design) |
| `VAPI_API_KEY` | yes | Vapi private API key for the REST API |
| `VAPI_ASSISTANT_ID` | yes | Fallback assistant for unknown inbound callers |
| `HINDSIGHT_API_KEY` | yes | Hindsight memory |
| `BRAVE_API_KEY` | yes | Brave Search API key for the web search tool |
| `VAPI_INBOUND_ASSISTANT_ID_US`, `VAPI_INBOUND_ASSISTANT_ID_CH` | no | Country-specific assistants for unknown inbound callers; US also covers Canada |
| `VAPI_ASSISTANT_ID_US`, `VAPI_PHONE_NUMBER_ID_US` | for US/Canada sign-up | Defaults assigned to customers who sign up during a call |
| `VAPI_ASSISTANT_ID_CH`, `VAPI_PHONE_NUMBER_ID_CH` | for Swiss sign-up | Defaults assigned to customers who sign up during a call |
| `SMTP_USER`, `SMTP_PASSWORD`, `TEAM_EMAIL` | for team email | Google Workspace SMTP credentials and notification recipient |
| `HEALTHCHECKS_PING_URL` | no | Dispatcher monitoring endpoint |
| `SUPABASE_JWT_SECRET` | no | Legacy Supabase projects using HS256 |
| `SUPABASE_SECRET_KEY` | yes | Backend-only Supabase secret key; uploads, deletes and signs 5-minute playback links for call recordings in the private `call-recordings` bucket. Never expose it to the browser |
| `LOCAL_LOGGING`, `LOG_STDOUT` | no | Local rotating log file and optional stdout output |

See [`.env.example`](../.env.example) for placeholders and conditional settings.
