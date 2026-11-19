# Albert

[![Tests](https://github.com/mcbuehler/albert/actions/workflows/tests.yml/badge.svg)](https://github.com/mcbuehler/albert/actions/workflows/tests.yml)

Backend and assistant configurations for **Albert**, a Vapi voice assistant with long-term memory (Hindsight). Albert answers inbound calls, places outbound and scheduled callbacks, archives call records and available recordings and transcripts, and provides a staff page to manage customers and review calls.

- **Memory:** one Hindsight bank per customer, created at onboarding. Customers' calls never run without their memory; non-customers get none (no consent).
- **Callbacks:** scheduled in the customer's local time (calling hours 09:00–22:00, `config.CALLING_START` / `CALLING_END`), dialled by a dispatcher that Vercel Cron runs every minute; up to 4 attempts (unanswered calls, including voicemail, are retried after 60 minutes), one call per Vapi phone number and per customer at a time.
- **Staff page** (`/`): customer list and detail panel, Calls tab (all archived calls and upcoming callbacks: search, status filter, sort, paging, a "now" line between upcoming and past, recording/transcript icons; a detail dialog with recording, transcript and staff notes that staff can add to), schedule / call now / cancel, onboarding, editing (name, time zone, default assistant), review of inbound sign-ups, danger zone (erase memory, delete customer). Sign-in by Supabase magic link.

## Quickstart

Use [uv](https://docs.astral.sh/uv/) with Python 3.12, the version used by CI. The [test suite](#tests) can run with no provider accounts.

```bash
git clone https://github.com/mcbuehler/albert.git
cd albert
uv sync --locked --python 3.12
cp .env.example .env
```

1. Create a Supabase project dedicated to Albert. Run all 15 SQL files in [`migrations/`](migrations/) in numeric order in the SQL editor. Create a private Storage bucket named `call-recordings` and check that its upload limit fits your recordings. Albert caps each upload at 100 MiB.
2. In Supabase Auth, enable email magic links, disable public sign-ups, and invite your staff users. Set the Site URL to your deployment URL and add `http://localhost:8000/**` for local redirects. Configure custom SMTP to send magic links to invited staff. All signed-in users in this project have staff access to Albert. [Supabase redirects](https://supabase.com/docs/guides/auth/redirect-urls) · [Custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp)
3. Fill in [`.env`](#environment-variables) with your own service credentials. Use the Supabase transaction pooler connection string and separate secrets for `REL8_API_AUTH_TOKEN` and `CRON_SECRET`. Create a Hindsight Cloud API key and a Brave Search API key for memory and web search.
4. Follow the [Vapi setup guide](docs/vapi-setup.md) to create the tools and assistants, wire up a phone number, and set the assistant IDs in your environment. Vapi needs a public HTTPS URL for webhooks and tools. Use your [deployment](#deployment) or a tunnel to the local server for test calls.

```bash
uv run --locked --python 3.12 --env-file .env gunicorn main:app --bind 127.0.0.1:8000 --timeout 150
```

Open `http://localhost:8000` for the staff page. `GET /api/health` checks that required settings are present and Postgres is reachable. Follow the [smoke test](docs/vapi-setup.md#smoke-test) to verify staff login, inbound routing, callbacks, memory and playback with your own test number.

## Architecture

| Path | What |
|---|---|
| `albert/` | Flask app: `routes.py` (endpoints), `dispatcher.py`, `scheduling.py`, `vapi.py`, `memory.py`, `auth.py`, `db.py`, `archive.py` (call archive cron), `storage.py` (Supabase Storage), `config.py` (business rules) … |
| `main.py` | Entry point for Vercel / gunicorn (`main:app`) |
| `public/` | Staff page: plain HTML/CSS and ES modules, no build step |
| `migrations/` | Plain SQL for Supabase Postgres, applied in order |
| `tests/` | `unittest` suite: external APIs mocked, database layer tested against throwaway local PostgreSQL |
| [`vapi/assistants/`](vapi/assistants/) | English and German Albert Live templates, including both inbound variants |
| `docs/` | [Architecture](docs/architecture.md), [Vapi setup](docs/vapi-setup.md), [archive schema](docs/call-archive-schema.md), [archive design history](docs/call-archive-plan.md), and [conversation-flow research](docs/vapi-conversation-flow-research.md) |

## Environment variables

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

See [`.env.example`](.env.example) for placeholders and conditional settings.

## Call archive

The archive job scans the calls visible to your Vapi API key, including unknown and rejected callers. It stores call metadata and spoken transcripts in Postgres and available recordings in the private Supabase bucket. This runs independently of Hindsight memory and its onboarding consent. Use a dedicated Vapi organization if the calls in another project should stay out of this archive.

The reconciliation window defaults to 14 days to match the original Vapi account's retention. Set `ARCHIVE_PROVIDER_RETENTION_DAYS` in [`albert/config.py`](albert/config.py) for your account. Albert keeps its archived copies independently of Vapi's retention. Deleting a customer removes their archive entries and queues recordings for deletion. Erasing Hindsight memory keeps the archive. To disable archive capture, set `ARCHIVE_CAPTURE_ENABLED = False`. See the [archive schema](docs/call-archive-schema.md) for details.

## Docker development

Build the image from the repository root and mount your configured `.env` as a read-only file to start the app.

```bash
docker build -t albert-dev .
docker run --rm -p 127.0.0.1:8000:8000 \
  --mount "type=bind,source=$PWD/.env,target=/run/albert.env,readonly" \
  albert-dev uv run --no-sync --env-file /run/albert.env gunicorn main:app --bind 0.0.0.0:8000 --timeout 150
```

With no command, the image opens a development shell with a throwaway PostgreSQL instance for the test suite. Use `docker run --rm -it albert-dev` to open that shell.

## Tests

```bash
uv run --locked --python 3.12 ruff check .
uv run --locked --python 3.12 python -m unittest discover -s tests
```

Without `TEST_DATABASE_URL`, only the database integration module is skipped. The Docker development shell sets up a test database automatically. For a local run, point `TEST_DATABASE_URL` at a throwaway PostgreSQL database, for example `postgresql://postgres:postgres@localhost:5432/albert_test`. The integration tests rebuild its `public` schema.

GitHub Actions runs the entire suite against a PostgreSQL 17 service on every push and pull request
(`.github/workflows/tests.yml`).

## Deployment

Import the repository as a Vercel project and set the [environment variables](#environment-variables) there. The supplied [`vercel.json`](vercel.json) runs the dispatcher and call archive every minute, which requires a plan that supports minute-level cron jobs. Vercel sends `CRON_SECRET` in the bearer header. On another host, schedule authenticated GET requests to `/api/cron/dispatch` and `/api/cron/archive`. Staff can run either job from the **Debug** tab. [Vercel cron limits](https://vercel.com/docs/schedules/cron-jobs/usage-and-pricing)

Add the deployment URL to Supabase Auth redirects and configure Vapi to use that URL. Run the [smoke test](docs/vapi-setup.md#smoke-test) after deployment.

## License

Albert is available under the [MIT License](LICENSE).

## Acknowledgements

Thank you to OpenAI and Anthropic for providing API credits, and to the ETH Entrepreneur Club for its
[RocketHub accelerator](https://www.entrepreneur-club.org/rockethub). Thanks to everyone who used and
tested Albert and helped us improve it.

The backend started from [Vercel's Flask example](https://github.com/vercel/vercel/tree/main/examples/flask).
