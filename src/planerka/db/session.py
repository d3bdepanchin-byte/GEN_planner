from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """Сессия с коммитом при выходе и откатом при исключении."""
    engine = create_engine(url)
    session = Session(engine)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
