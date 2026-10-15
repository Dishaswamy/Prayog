# One image, three processes (SADD ADR-02): the Runtime, the API and the Worker.
#   runtime: uvicorn steward_runtime.app:app --host 0.0.0.0 --port 8100
#   api:     python -m prayog_steward.entrypoints api --host 0.0.0.0 --ports <module:factory>
#   worker:  python -m prayog_steward.entrypoints worker --ports <module:factory>
#   migrate: alembic upgrade head   (with STEWARD_DATABASE_URL = an owner/admin URL)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
RUN pip install . uvicorn

COPY tools ./tools
COPY eval ./eval

RUN useradd --system --uid 10001 steward
USER steward

EXPOSE 8000 8100
CMD ["uvicorn", "steward_runtime.app:app", "--host", "0.0.0.0", "--port", "8100"]
