"""Shared dependencies. One Container for database reads; jobs build their own (see jobs.py)."""

from functools import lru_cache

from app.api.jobs import JobManager
from app.bootstrap import Container


@lru_cache
def get_container() -> Container:
    return Container()


@lru_cache
def get_jobs() -> JobManager:
    return JobManager()
