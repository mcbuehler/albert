# Setup

Use [uv](https://docs.astral.sh/uv/) with Python 3.12, the version used by CI. The [test suite](development.md#tests) can run with no provider accounts.
Review the [known production issues](known-production-issues.md) before connecting a number for real callers.

For a working deployment, create accounts with:

- [Supabase](https://supabase.com/dashboard): create a project and obtain its project URL, publishable and secret API keys, and transaction pooler connection string from the dashboard. See [API keys](https://supabase.com/docs/guides/api/api-keys) and [database connections](https://supabase.com/docs/guides/database/connecting-to-postgres).
- [Hindsight Cloud](https://ui.hindsight.vectorize.io): create an [API key](https://docs.hindsight.vectorize.io/cloud/api-keys/). Albert creates each customer's memory bank automatically during onboarding.
- [Vapi](https://dashboard.vapi.ai/signup): create an organization and obtain a [private API key](https://docs.vapi.ai/security-and-privacy/api-keys). Calls require a GPT-Live-compatible phone connection and an outbound-capable number for callbacks; see the [Vapi setup guide](vapi-setup.md).
- [Brave Search API](https://api-dashboard.search.brave.com/): create an account and API key with access to the [LLM Context endpoint](https://api-dashboard.search.brave.com/documentation/services/llm-context) for web search.

```bash
git clone https://github.com/mcbuehler/albert.git
cd albert
uv sync --locked --python 3.12
cp .env.example .env
```

1. Create a Supabase project dedicated to Albert. Run all 15 SQL files in [`migrations/`](../migrations/) in numeric order in the SQL editor. Create a private Storage bucket named `call-recordings` and check that its upload limit fits your recordings. Albert caps each upload at 100 MiB.
2. In Supabase Auth, enable email magic links, disable public sign-ups, and invite your staff users. Set the Site URL to your deployment URL and add `http://localhost:8000/**` for local redirects. Configure custom SMTP to send magic links to invited staff. All signed-in users in this project have staff access to Albert. [Supabase redirects](https://supabase.com/docs/guides/auth/redirect-urls) · [Custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp)
3. Fill in [`.env`](configuration.md) with your own service credentials. Use the Supabase transaction pooler connection string and separate secrets for `REL8_API_AUTH_TOKEN` and `CRON_SECRET`. Create a Hindsight Cloud API key and a Brave Search API key for memory and web search.
4. Follow the [Vapi setup guide](vapi-setup.md) to create the tools and assistants, wire up a phone number, and set the assistant IDs in your environment. Vapi needs a public HTTPS URL for webhooks and tools. Use your [deployment](deployment.md) or a tunnel to the local server for test calls.

```bash
uv run --locked --python 3.12 --env-file .env gunicorn main:app --bind 127.0.0.1:8000 --timeout 150
```

Open `http://localhost:8000` for the staff page. `GET /api/health` checks that required settings are present and Postgres is reachable. Follow the [smoke test](vapi-setup.md#smoke-test) to verify staff login, inbound routing, callbacks, memory and playback with your own test number.
