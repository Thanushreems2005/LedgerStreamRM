"""
Money-conservation invariant tests for ledger_consumer.apply_transfer().

SAFETY
- Runs ONLY against LEDGER_TEST_DSN. Never reads DATABASE_URL / PG_DSN / .env.
- Refuses non-local hosts unless LEDGER_TEST_ALLOW_REMOTE=1.
- Touches only accounts named INV_* and events named event_inv_*.

RUN
  docker run -d --name ls-test-pg -e POSTGRES_PASSWORD=t -p 5440:5432 postgres:16
  psql postgresql://postgres:t@localhost:5440/postgres -f init-db/schema.sql
  LEDGER_TEST_DSN=postgresql://postgres:t@localhost:5440/postgres \
      python -m unittest tests.test_ledger_invariants -v

EXPECTED (after the apply_transfer fix)
  test_random_mixed_transfers_conserve_total ........ PASS
  test_vanished_receiver_does_not_destroy_money ..... PASS
  (Before the fix, the second test FAILS with "MONEY LOST".)
"""
import os
import random
import sys
import unittest
import uuid

import psycopg2
from psycopg2.extensions import cursor as _BaseCursor

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "consumer")))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fraud")))

# Windows Application Control may block confluent_kafka's DLL on the host.
# apply_transfer() never touches Kafka, so stub it only if the real one fails.
try:
    import confluent_kafka  # noqa: F401
except ImportError:
    import types

    for _name in [m for m in sys.modules if m.startswith("confluent_kafka")]:
        del sys.modules[_name]
    _stub = types.ModuleType("confluent_kafka")

    class _Unused:
        def __init__(self, *a, **k):
            raise RuntimeError("Kafka stubbed out in tests")

    _stub.Consumer = _Unused
    _stub.Producer = _Unused
    _stub.KafkaException = Exception
    _stub.KafkaError = Exception
    sys.modules["confluent_kafka"] = _stub

import ledger_consumer  # type: ignore  # noqa: E402

TEST_DSN = os.environ.get("LEDGER_TEST_DSN")
ACCOUNTS = ["INV_A", "INV_B", "INV_C", "INV_D", "INV_E"]
START_BALANCE = 1000.00


def _dsn_is_safe(dsn: str) -> bool:
    if os.environ.get("LEDGER_TEST_ALLOW_REMOTE") == "1":
        return True
    d = dsn.lower()
    return "localhost" in d or "127.0.0.1" in d


class HookCursor(_BaseCursor):
    """Cursor that can run a callback right before the credit UPDATE.

    Used to simulate the receiver row vanishing AFTER apply_transfer's
    existence check but BEFORE the credit executes (deterministic race).
    """

    before_credit = None

    def execute(self, query, vars=None):
        if HookCursor.before_credit is not None and "balance = balance +" in str(query):
            hook, HookCursor.before_credit = HookCursor.before_credit, None
            hook()
        return super().execute(query, vars)


@unittest.skipUnless(TEST_DSN, "set LEDGER_TEST_DSN to run (never uses the live DB)")
class TestMoneyConservation(unittest.TestCase):
    def setUp(self):
        if not _dsn_is_safe(TEST_DSN):
            self.fail("LEDGER_TEST_DSN is not local. Set LEDGER_TEST_ALLOW_REMOTE=1 to override.")
        self.conn = psycopg2.connect(TEST_DSN, cursor_factory=HookCursor)
        self.conn.autocommit = False
        self.side = psycopg2.connect(TEST_DSN)  # separate session for race simulation
        self.side.autocommit = True
        with self.side.cursor() as c:
            c.execute("SET lock_timeout = '500ms'")  # delete must fail fast if row is locked
        HookCursor.before_credit = None
        self._cleanup()
        with self.conn.cursor() as cur:
            for a in ACCOUNTS:
                cur.execute(
                    "INSERT INTO accounts (account_id, balance) VALUES (%s, %s)",
                    (a, START_BALANCE),
                )
        self.conn.commit()

    def tearDown(self):
        HookCursor.before_credit = None
        self.conn.rollback()
        self._cleanup()
        self.conn.close()
        self.side.close()

    def _cleanup(self):
        c = psycopg2.connect(TEST_DSN)
        try:
            with c, c.cursor() as cur:
                cur.execute("DELETE FROM processed_events WHERE event_id LIKE 'event_inv_%'")
                cur.execute("DELETE FROM transactions_log WHERE event_id LIKE 'event_inv_%'")
                cur.execute("DELETE FROM accounts WHERE account_id LIKE 'INV_%'")
        finally:
            c.close()

    def _balance(self, account):
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM accounts WHERE account_id = %s", (account,))
            row = cur.fetchone()
            return None if row is None else row[0]

    def _total(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE account_id LIKE 'INV_%'")
            return cur.fetchone()[0]

    @staticmethod
    def _event(frm, to, amount):
        return {
            "event_id": f"event_inv_{uuid.uuid4().hex[:16]}",
            "from_account": frm,
            "to_account": to,
            "amount": amount,
            "timestamp": "2026-10-03T10:00:00+00:00",
        }

    # ------------------------------------------------------------------
    def test_random_mixed_transfers_conserve_total(self):
        """200 random transfers (APPROVE/VERIFY/HOLD, overdrafts, unknown
        accounts). Total across INV_* accounts must never change."""
        rng = random.Random(42)
        before = self._total()
        outcomes = {"applied": 0, "held": 0, "blocked": 0, "raised": 0}
        pool = ACCOUNTS + ["INV_GHOST"]  # GHOST does not exist
        plans = [("LOW", "APPROVE"), ("MEDIUM", "VERIFY"), ("HIGH", "HOLD")]

        for _ in range(200):
            frm, to = rng.sample(pool, 2)
            amount = round(rng.uniform(1, 1500), 2)  # sometimes > balance
            level, action = rng.choice(plans)
            try:
                status = ledger_consumer.apply_transfer(self.conn, self._event(frm, to, amount), level, action)
                outcomes[status] += 1
            except Exception:
                self.conn.rollback()  # exactly what the consumer main loop does
                outcomes["raised"] += 1
            self.assertEqual(self._total(), before, f"total drifted after {frm}->{to} {amount} {action}")

        # make sure the run actually exercised every path
        for key, n in outcomes.items():
            self.assertGreater(n, 0, f"path never exercised: {key} ({outcomes})")

    def test_vanished_receiver_does_not_destroy_money(self):
        """Receiver row disappears between the existence check and the credit.
        Correct behaviour: raise (and roll back) OR credit someone.
        Silent debit with a 0-row credit = money destroyed."""
        sender_before = self._balance("INV_A")
        amount = 250.00

        def delete_receiver():
            try:
                with self.side.cursor() as cur:
                    cur.execute("DELETE FROM accounts WHERE account_id = 'INV_B'")
            except psycopg2.errors.LockNotAvailable:
                pass  # receiver row is locked by the transfer: that IS the fix working

        HookCursor.before_credit = delete_receiver
        raised = False
        try:
            ledger_consumer.apply_transfer(self.conn, self._event("INV_A", "INV_B", amount), "LOW", "APPROVE")
        except Exception:
            self.conn.rollback()
            raised = True

        sender_after = self._balance("INV_A")
        receiver_after = self._balance("INV_B")

        if raised:
            self.assertEqual(sender_after, sender_before, "raised but sender was still debited")
        else:
            debited = sender_before - sender_after
            self.assertIsNotNone(
                receiver_after,
                f"MONEY LOST: apply_transfer committed, sender debited {debited}, "
                f"credit hit 0 rows (receiver gone). No exception raised.",
            )


if __name__ == "__main__":
    unittest.main()