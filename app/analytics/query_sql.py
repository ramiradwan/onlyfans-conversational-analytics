"""Apply one question budget to a read-only SQL connection."""

from contextlib import contextmanager
from app.persistence import sqlite_api


@contextmanager
def bounded_sql(connection, budget):
    budget.check()
    connection.execute(f"PRAGMA busy_timeout={max(1, int(budget.remaining_seconds() * 1000))}")
    connection.execute("PRAGMA query_only=ON")
    interrupted = []

    def progress():
        try:
            budget.check()
        except Exception as error:
            interrupted.append(error)
            return 1
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        yield connection
        budget.check()
    except sqlite_api.OperationalError:
        if interrupted:
            raise interrupted[0] from None
        budget.check()
        raise
    finally:
        connection.set_progress_handler(None, 0)
