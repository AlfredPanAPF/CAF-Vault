"""Shared test setup. CAF_DB_URL must be set before graph.config is imported
(it reads env at import); the default is the local caf_graph_test database.
Override CAF_DB_URL to run test files in parallel against separate databases:

    CAF_DB_URL=postgresql:///caf_test_x uv run pytest tests/test_quality.py
"""
import os
import subprocess

os.environ.setdefault("CAF_DB_URL", "postgresql:///caf_graph_test")

import pytest

from graph import config, db


def _dbname():
    return os.environ["CAF_DB_URL"].rsplit("/", 1)[1]


@pytest.fixture(scope="session")
def database():
    name = _dbname()
    subprocess.run(["dropdb", name], capture_output=True)
    subprocess.run(["createdb", name], capture_output=True)
    db.migrate()
    with db.connect() as con:
        con.execute("insert into registry_sec (cik, ticker, title) values "
                    "(1045810, 'NVDA', 'NVIDIA CORP'), "
                    "(2488, 'AMD', 'ADVANCED MICRO DEVICES INC')")
        con.commit()
    yield
    subprocess.run(["dropdb", name], capture_output=True)


@pytest.fixture
def con(database, tmp_path, monkeypatch):
    monkeypatch.setenv("CAF_ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path)
    c = db.connect()
    yield c
    c.rollback()
    c.close()
