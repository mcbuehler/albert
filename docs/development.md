# Development

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
