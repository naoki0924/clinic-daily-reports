import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="clinic-test-")
os.environ["APP_DATA_DIR"] = _TMP
os.environ["APP_SECRET_KEY"] = "test-secret-key-for-unit-tests-only-0123456789"
os.environ["RUN_SCHEDULER"] = "0"
os.environ.pop("TALKNOTE_LOGIN_ID", None)
os.environ.pop("TALKNOTE_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.bootstrap import seed  # noqa: E402
from app.db import Base, engine  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    seed()
    from app import pipeline
    pipeline._wb_cache.clear()
    yield
