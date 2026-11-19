FROM python:3.12-slim

# Install system updates
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql \
 && rm -rf /var/lib/apt/lists/*

# Local throwaway Postgres for the db.py tests (never Supabase): user postgres/postgres, database albert_test
RUN pg_ctlcluster 17 main start \
 && su postgres -c "psql -qc \"ALTER USER postgres PASSWORD 'postgres'\" && createdb albert_test" \
 && pg_ctlcluster 17 main stop
ENV TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/albert_test"

# Install uv directly using the official installer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy dependency definition files first for optimal docker layer caching
COPY pyproject.toml uv.lock* ./

# Install dependencies into the virtual environment and reject a stale lockfile
RUN uv sync --locked --no-cache

# Place the virtualenv on PATH so you don't need to manually run `source .venv/bin/activate`
ENV PATH="/app/.venv/bin:$PATH"

# Copy the rest of the application code
COPY . .

# Expose port 8000
EXPOSE 8000

# Default command: start the test Postgres, then a shell
CMD ["bash", "-c", "pg_ctlcluster 17 main start && exec bash"]
