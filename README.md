# Prayog

Prayog is an IIT Kanpur research-platform project. The current repository contains a FastAPI backend with PostgreSQL persistence, account/access-request APIs, and an ML Flow project/model/experiment lifecycle with artifact-backed rebuild tracking.

## Backend setup

1. Create a root-level `.env` file with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, and a strong `JWT_SECRET_KEY`.
2. Start PostgreSQL using `docker compose up -d postgres`.
3. Install dependencies and apply migrations:

   ```sh
   cd backend
   python -m pip install -r requirements.txt
   alembic upgrade head
   uvicorn main:app --reload
   ```

4. Check `http://localhost:8000/health` and use `http://localhost:8000/docs` for the API schema.

## ML Flow

See [the ML Flow lifecycle guide](backend/ML_FLOW.md) for experiment/run states, artifact upload and integrity verification, snapshot retrieval, rebuild tracking, CLI commands, the isolated rebuild worker, and current implementation boundaries. The [AI integration contract](backend/AI_INTEGRATION.md) documents the transactional outbox and the remaining requirements before enabling Steward diagnosis or optimization consumers.

ML Flow artifact files are stored in a private local directory by default. Configure `PRAYOG_ARTIFACT_ROOT` to a persistent private volume in deployment. Do not commit `.env`, bearer tokens, source archives, model weights, or diagnostic logs.
