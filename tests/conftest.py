from pathlib import Path

import pytest

from ledgerline import db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "test.db"
