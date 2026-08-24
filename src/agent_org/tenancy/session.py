"""Entity-scoped database sessions.

Every business query runs inside ``entity_session``: a transaction with
``app.entity_id`` set via ``SET LOCAL`` (``set_config(..., true)``), which
the row-level-security policies read with the one-argument
``current_setting('app.entity_id')``. A connection that never entered a
session gets a raised error on any RLS-protected query — loud failure,
never another entity's rows and never a silently empty result
(docs/multi-entity.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg


@contextmanager
def entity_session(conn: psycopg.Connection, entity_id: str) -> Iterator[psycopg.Connection]:
    with conn.transaction():
        conn.execute("SELECT set_config('app.entity_id', %s, true)", (entity_id,))
        yield conn
        # SET LOCAL alone would survive while an enclosing transaction is
        # still open, so the scope is cleared on the way out: leaving the
        # block must never leave one entity's scope armed for the next
        # query. (On the error path the transaction is already unusable and
        # is about to roll back, which clears it anyway.)
        conn.execute("SELECT set_config('app.entity_id', '', true)")
