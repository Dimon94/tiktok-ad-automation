"""真实 PostgreSQL 回归：目录发布等锁/软超时不能泄漏事务和连接。"""

import signal
from time import monotonic, sleep
from uuid import uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.core.db import engine
from app.modules.ads import tasks


def test_directory_transaction_bounds_lock_wait(monkeypatch):
    key = uuid4().int % (2**63)
    monkeypatch.setattr(tasks, "_LOCK_TIMEOUT", "150ms")
    with engine.connect() as blocker:
        blocker.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        started = monotonic()
        with pytest.raises(OperationalError, match="lock timeout"):
            with tasks._directory_session(engine) as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
                )
        assert monotonic() - started < 3
    with tasks._directory_session(engine) as session:
        assert session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        ).scalar_one()


def test_soft_timeout_during_savepoint_closes_backend():
    # 模拟线上：信号打断 psycopg 等待，savepoint 回滚会再报 command in progress。
    # 即使回滚失败，外层也必须关闭物理连接，让服务器释放事务持有的锁。
    def interrupt(_signum, _frame):
        raise SoftTimeLimitExceeded()

    previous = signal.signal(signal.SIGALRM, interrupt)
    pid = None
    try:
        with pytest.raises((SoftTimeLimitExceeded, OperationalError)):
            with tasks._directory_session(engine) as session:
                pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
                with session.begin_nested():
                    signal.setitimer(signal.ITIMER_REAL, 0.15)
                    session.execute(text("SELECT pg_sleep(3)"))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert pid is not None
    # 服务器要等当前语句返回才能察觉断连；轮询真实状态，避免把断连传播当成同步操作。
    until = monotonic() + 5
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        while True:
            count = connection.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": pid},
            ).scalar_one()
            if count == 0 or monotonic() >= until:
                break
            sleep(0.05)
    assert count == 0


def test_directory_transaction_bounds_total_time_across_short_statements(monkeypatch):
    # 每条 SQL 都很快也不能让总事务超过预算，否则仍会撞上 Celery 信号。
    monkeypatch.setattr(tasks, "_TRANSACTION_TIMEOUT", "150ms", raising=False)
    started = monotonic()
    with pytest.raises(DBAPIError, match="transaction timeout"):
        with tasks._directory_session(engine) as session:
            assert (
                session.connection()
                .execute(text("SHOW transaction_timeout"))
                .scalar_one()
                == "150ms"
            )
            # PG取消信号传播并不保证恰好第4条SQL前到达；每条仍远短于150ms，
            # 有界循环证明限制的是整个事务，不能改成单条长SQL超时。
            for _ in range(32):
                session.connection().execute(text("SELECT pg_sleep(0.06)"))
    assert monotonic() - started < 2
