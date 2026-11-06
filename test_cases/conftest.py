import sys
from pathlib import Path

# The tests import the backend modules (main, models, logic.*, ...) and share helpers under
# test_cases/helpers, so both the backend folder and the repo root must be importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for path in (str(BACKEND_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
