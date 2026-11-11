#!/bin/sh
# Start Prayog on a host that runs one process per service, such as a Render web service.
# docker-compose.deploy.yml runs each process as its own service; use this only where that is not possible.
#
#     cd backend && sh scripts/start_single_container.sh
#
# Runs, beside the API:
#   - the scheduler tick (exactly one; restarted if it exits), without which a dead node is never marked DOWN;
#   - when the Steward is configured (STEWARD_DB_PASSWORD is set): its database setup, then its worker.
# Run only one copy of this script per deployment: a second would start a second tick.
#
# PORT is set by the host (default 8000). WEB_CONCURRENCY sets the API workers (default 1, for small instances).

supervise() {
    while true; do
        "$@"
        echo "start_single_container: '$*' exited with code $?; restarting in 5 s" >&2
        sleep 5
    done
}

supervise python -m logic.gpu.scheduler.run &
if [ -n "${STEWARD_DB_PASSWORD:-}" ] && [ "${STEWARD_ENABLED:-true}" != "false" ]; then
    # Idempotent: creates or updates the Steward's role, runs its migrations and grants. On a host with
    # no shell into the database, this is the only place it can run. If it fails, the API starts anyway
    # and runs without the Steward (install_steward logs why).
    python -m scripts.setup_steward || echo "start_single_container: Steward setup failed; see above" >&2
    supervise python -m integration.steward_worker &
fi

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WEB_CONCURRENCY:-1}"
