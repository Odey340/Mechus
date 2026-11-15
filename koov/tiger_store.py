"""
Tiger Data (TimescaleDB on Tiger Cloud) persistence + analytics for Koov.

Koov keeps its fast local per-meeting store (koov/meeting_store.py) for live
retrieval. This module *mirrors* everything into Tiger Data so meetings become
queryable time-series data with plain SQL:

  koov_transcript_chunks   hypertable  every transcript line (who, what, when, topic, words)
  koov_meeting_snapshots   hypertable  decisions / action items / open questions over time
  koov_events              hypertable  agent events (e.g. wake -> spoken answer latency)
  koov_speaker_activity_1m continuous aggregate: words + lines per speaker per minute

Writes are batched on a background thread and never block or crash the agent:
if Tiger Data is not configured or unreachable, Koov keeps working exactly as before.

Configure (see docs/TIGER_DATA.md):
  TIGER_DATABASE_URL=postgres://tsdbadmin:<PASSWORD>@<HOST>:<PORT>/tsdb?sslmode=require
  (aliases: KOOV_TIGER_DATABASE_URL, TIMESCALE_SERVICE_URL)
  KOOV_TIGER_ENABLED=1               # default: on when a URL is set
  KOOV_TIGER_RETENTION_DAYS=          # optional: drop raw transcript chunks older than N days
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("koov.tiger")

_URL_ENV_KEYS = ("TIGER_DATABASE_URL", "KOOV_TIGER_DATABASE_URL", "TIMESCALE_SERVICE_URL")

# ---------------------------------------------------------------------------
# Schema (idempotent; each statement runs on its own in autocommit mode)
# ---------------------------------------------------------------------------

_HYPERTABLES: list[tuple[str, str, str, str]] = [
    # (table, modern CREATE TABLE ... WITH (tsdb.hypertable ...), legacy CREATE TABLE, segmentby)
    (
        "koov_transcript_chunks",
        """
        CREATE TABLE IF NOT EXISTS koov_transcript_chunks (
            ts              TIMESTAMPTZ NOT NULL,
            meeting_id      TEXT        NOT NULL,
            speaker_id      TEXT        NOT NULL,
            speaker_display TEXT,
            text            TEXT        NOT NULL,
            word_count      INTEGER     NOT NULL DEFAULT 0,
            chunk_id        INTEGER,
            topic           TEXT        NOT NULL DEFAULT 'general',
            tags            TEXT[]      NOT NULL DEFAULT '{}',
            content_hash    TEXT
        ) WITH (
            tsdb.hypertable,
            tsdb.partition_column = 'ts',
            tsdb.segmentby = 'meeting_id',
            tsdb.orderby = 'ts DESC',
            tsdb.chunk_interval = '1 day'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS koov_transcript_chunks (
            ts TIMESTAMPTZ NOT NULL, meeting_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
            speaker_display TEXT, text TEXT NOT NULL, word_count INTEGER NOT NULL DEFAULT 0,
            chunk_id INTEGER, topic TEXT NOT NULL DEFAULT 'general',
            tags TEXT[] NOT NULL DEFAULT '{}', content_hash TEXT
        )
        """,
        "meeting_id",
    ),
    (
        "koov_meeting_snapshots",
        """
        CREATE TABLE IF NOT EXISTS koov_meeting_snapshots (
            ts               TIMESTAMPTZ NOT NULL,
            meeting_id       TEXT        NOT NULL,
            n_decisions      INTEGER     NOT NULL DEFAULT 0,
            n_action_items   INTEGER     NOT NULL DEFAULT 0,
            n_open_questions INTEGER     NOT NULL DEFAULT 0,
            n_postponed      INTEGER     NOT NULL DEFAULT 0,
            snapshot         JSONB       NOT NULL
        ) WITH (
            tsdb.hypertable,
            tsdb.partition_column = 'ts',
            tsdb.segmentby = 'meeting_id',
            tsdb.orderby = 'ts DESC',
            tsdb.chunk_interval = '7 days'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS koov_meeting_snapshots (
            ts TIMESTAMPTZ NOT NULL, meeting_id TEXT NOT NULL,
            n_decisions INTEGER NOT NULL DEFAULT 0, n_action_items INTEGER NOT NULL DEFAULT 0,
            n_open_questions INTEGER NOT NULL DEFAULT 0, n_postponed INTEGER NOT NULL DEFAULT 0,
            snapshot JSONB NOT NULL
        )
        """,
        "meeting_id",
    ),
    (
        "koov_events",
        """
        CREATE TABLE IF NOT EXISTS koov_events (
            ts         TIMESTAMPTZ      NOT NULL,
            meeting_id TEXT             NOT NULL,
            kind       TEXT             NOT NULL,
            value_ms   DOUBLE PRECISION,
            meta       JSONB            NOT NULL DEFAULT '{}'
        ) WITH (
            tsdb.hypertable,
            tsdb.partition_column = 'ts',
            tsdb.segmentby = 'kind',
            tsdb.orderby = 'ts DESC',
            tsdb.chunk_interval = '7 days'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS koov_events (
            ts TIMESTAMPTZ NOT NULL, meeting_id TEXT NOT NULL, kind TEXT NOT NULL,
            value_ms DOUBLE PRECISION, meta JSONB NOT NULL DEFAULT '{}'
        )
        """,
        "kind",
    ),
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS koov_chunks_meeting_ts_idx ON koov_transcript_chunks (meeting_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS koov_chunks_speaker_ts_idx ON koov_transcript_chunks (speaker_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS koov_snapshots_meeting_ts_idx ON koov_meeting_snapshots (meeting_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS koov_events_meeting_ts_idx ON koov_events (meeting_id, ts DESC)",
]

_CAGG_SQL = [
    """
    CREATE MATERIALIZED VIEW IF NOT EXISTS koov_speaker_activity_1m
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
    SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
           meeting_id,
           speaker_id,
           count(*)        AS lines,
           sum(word_count) AS words
    FROM koov_transcript_chunks
    GROUP BY bucket, meeting_id, speaker_id
    WITH NO DATA
    """,
    """
    SELECT add_continuous_aggregate_policy('koov_speaker_activity_1m',
        start_offset      => INTERVAL '30 days',
        end_offset        => INTERVAL '1 minute',
        schedule_interval => INTERVAL '5 minutes',
        if_not_exists     => TRUE)
    """,
]


def database_url() -> Optional[str]:
    for key in _URL_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


def is_enabled() -> bool:
    flag = (os.environ.get("KOOV_TIGER_ENABLED") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return database_url() is not None


def _to_dt(ts: Optional[float]) -> datetime:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)


def _connect(autocommit: bool = True):
    import psycopg  # psycopg 3 (pip install "psycopg[binary]")

    url = database_url()
    if not url:
        raise RuntimeError("TIGER_DATABASE_URL is not set")
    return psycopg.connect(url, autocommit=autocommit, connect_timeout=10, application_name="koov")


def ensure_schema(conn=None) -> dict[str, Any]:
    """Create hypertables, indexes, continuous aggregate and policies. Safe to run repeatedly."""
    own = conn is None
    conn = conn or _connect()
    report: dict[str, Any] = {"hypertables": [], "warnings": []}
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            for table, modern_sql, legacy_sql, segmentby in _HYPERTABLES:
                try:
                    cur.execute(modern_sql)
                except Exception as exc:  # older TimescaleDB without CREATE TABLE ... WITH (tsdb.*)
                    log.info("Modern hypertable syntax unavailable for %s (%s); using create_hypertable", table, exc)
                    cur.execute(legacy_sql)
                    cur.execute(
                        "SELECT create_hypertable(%s, 'ts', if_not_exists => TRUE, migrate_data => TRUE)",
                        (table,),
                    )
                    try:
                        cur.execute(
                            f"ALTER TABLE {table} SET (timescaledb.compress, "
                            f"timescaledb.compress_segmentby = '{segmentby}', "
                            f"timescaledb.compress_orderby = 'ts DESC')"
                        )
                    except Exception as cexc:
                        report["warnings"].append(f"compression on {table}: {cexc}")
                report["hypertables"].append(table)
                # Columnstore (hypercore) policy: older data is converted to compressed columnar storage.
                for policy_sql in (
                    f"CALL add_columnstore_policy('{table}', after => INTERVAL '7 days', if_not_exists => TRUE)",
                    f"SELECT add_compression_policy('{table}', INTERVAL '7 days', if_not_exists => TRUE)",
                ):
                    try:
                        cur.execute(policy_sql)
                        break
                    except Exception as pexc:
                        last_policy_err = pexc
                else:
                    report["warnings"].append(f"columnstore policy on {table}: {last_policy_err}")
            for sql in _INDEXES:
                cur.execute(sql)
            for sql in _CAGG_SQL:
                try:
                    cur.execute(sql)
                except Exception as exc:
                    report["warnings"].append(f"continuous aggregate: {exc}")
            retention = (os.environ.get("KOOV_TIGER_RETENTION_DAYS") or "").strip()
            if retention.isdigit() and int(retention) > 0:
                try:
                    cur.execute(
                        "SELECT add_retention_policy('koov_transcript_chunks', %s::interval, if_not_exists => TRUE)",
                        (f"{int(retention)} days",),
                    )
                except Exception as exc:
                    report["warnings"].append(f"retention policy: {exc}")
            report["toolkit"] = _try_enable_toolkit(cur)
    finally:
        if own:
            conn.close()
    return report


def _try_enable_toolkit(cur) -> bool:
    """TimescaleDB Toolkit adds hyperfunctions like percentile_agg / approx_percentile / stats_agg."""
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit")
        return True
    except Exception:
        return False


def _has_toolkit(cur) -> bool:
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb_toolkit'")
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Background writer
# ---------------------------------------------------------------------------


class TigerSink:
    """Batches rows on a daemon thread; reconnects with backoff; drops data rather than blocking Koov."""

    FLUSH_INTERVAL_S = 0.5
    MAX_BATCH = 500
    MAX_QUEUE = 20_000

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[str, tuple]]" = queue.Queue(maxsize=self.MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._schema_ready = False
        self._conn = None
        self.dropped = 0
        self.written = 0
        self.last_error: Optional[str] = None

    # -- public API ---------------------------------------------------------
    def record_chunk(
        self,
        meeting_id: str,
        speaker_id: str,
        text: str,
        ts: Optional[float] = None,
        *,
        chunk_id: Optional[int] = None,
        topic: str = "general",
        tags: Optional[list[str]] = None,
        speaker_display: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._put(
            "chunk",
            (
                _to_dt(ts),
                meeting_id,
                speaker_id or "unknown",
                speaker_display,
                text,
                len(text.split()),
                chunk_id,
                topic or "general",
                list(tags or []),
                content_hash,
            ),
        )

    def record_snapshot(self, meeting_id: str, snapshot: dict[str, Any], ts: Optional[float] = None) -> None:
        self._put(
            "snapshot",
            (
                _to_dt(ts or snapshot.get("updated_at")),
                meeting_id,
                len(snapshot.get("decisions") or []),
                len(snapshot.get("action_items") or []),
                len(snapshot.get("open_questions") or []),
                len(snapshot.get("postponed") or []),
                json.dumps(snapshot, ensure_ascii=False, default=str),
            ),
        )

    def record_event(
        self,
        meeting_id: str,
        kind: str,
        value_ms: Optional[float] = None,
        meta: Optional[dict[str, Any]] = None,
        ts: Optional[float] = None,
    ) -> None:
        self._put(
            "event",
            (_to_dt(ts), meeting_id or "unknown", kind, value_ms, json.dumps(meta or {}, default=str)),
        )

    def flush(self, timeout_s: float = 5.0) -> None:
        """Block until the queue drains (used by scripts/tests)."""
        deadline = time.time() + timeout_s
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(self.FLUSH_INTERVAL_S * 2)

    # -- internals ----------------------------------------------------------
    def _put(self, kind: str, row: tuple) -> None:
        self._ensure_thread()
        try:
            self._q.put_nowait((kind, row))
        except queue.Full:
            self.dropped += 1

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="koov-tiger-writer")
            self._thread.start()

    def _get_conn(self):
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = _connect(autocommit=True)
        if not self._schema_ready:
            report = ensure_schema(self._conn)
            self._schema_ready = True
            log.info("Tiger Data schema ready: %s", report)
        return self._conn

    def _run(self) -> None:
        backoff = 1.0
        while True:
            batch: list[tuple[str, tuple]] = []
            try:
                batch.append(self._q.get(timeout=1.0))
            except queue.Empty:
                continue
            deadline = time.time() + self.FLUSH_INTERVAL_S
            while len(batch) < self.MAX_BATCH and time.time() < deadline:
                try:
                    batch.append(self._q.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            try:
                self._write(batch)
                self.written += len(batch)
                self.last_error = None
                backoff = 1.0
            except Exception as exc:  # network blip, bad URL, etc.
                self.last_error = str(exc)
                log.warning("Tiger Data write failed (%d rows dropped): %s", len(batch), exc)
                self.dropped += len(batch)
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _write(self, batch: list[tuple[str, tuple]]) -> None:
        chunks = [r for k, r in batch if k == "chunk"]
        snaps = [r for k, r in batch if k == "snapshot"]
        events = [r for k, r in batch if k == "event"]
        conn = self._get_conn()
        with conn.transaction():
            with conn.cursor() as cur:
                if chunks:
                    cur.executemany(
                        "INSERT INTO koov_transcript_chunks (ts, meeting_id, speaker_id, speaker_display, text, "
                        "word_count, chunk_id, topic, tags, content_hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        chunks,
                    )
                if snaps:
                    cur.executemany(
                        "INSERT INTO koov_meeting_snapshots (ts, meeting_id, n_decisions, n_action_items, "
                        "n_open_questions, n_postponed, snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                        snaps,
                    )
                if events:
                    cur.executemany(
                        "INSERT INTO koov_events (ts, meeting_id, kind, value_ms, meta) "
                        "VALUES (%s, %s, %s, %s, %s::jsonb)",
                        events,
                    )

    def status(self) -> dict[str, Any]:
        return {
            "queued": self._q.qsize(),
            "written": self.written,
            "dropped": self.dropped,
            "last_error": self.last_error,
            "schema_ready": self._schema_ready,
        }


_sink: Optional[TigerSink] = None
_sink_lock = threading.Lock()


def get_sink() -> Optional[TigerSink]:
    """Shared writer, or None when Tiger Data is not configured (callers just skip)."""
    global _sink
    if not is_enabled():
        return None
    try:
        import psycopg  # noqa: F401
    except ImportError:
        log.warning("TIGER_DATABASE_URL is set but psycopg is not installed: pip install \"psycopg[binary]\"")
        return None
    if _sink is None:
        with _sink_lock:
            if _sink is None:
                _sink = TigerSink()
    return _sink


def record_event(meeting_id: str, kind: str, value_ms: Optional[float] = None, **meta: Any) -> None:
    """Fire-and-forget helper for agent/API code. Never raises."""
    try:
        sink = get_sink()
        if sink:
            sink.record_event(meeting_id, kind, value_ms, meta)
    except Exception:
        log.debug("Tiger record_event failed", exc_info=True)


# ---------------------------------------------------------------------------
# Analytics (plain SQL + TimescaleDB hyperfunctions)
# ---------------------------------------------------------------------------


def _rows(cur) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description]
    out = []
    for row in cur.fetchall():
        item = {}
        for c, v in zip(cols, row):
            if isinstance(v, datetime):
                v = v.isoformat()
            elif hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
                v = float(v)
            item[c] = v
        out.append(item)
    return out


def meeting_analytics(meeting_id: str, bucket: str = "1 minute") -> dict[str, Any]:
    """Speaker talk share, activity timeline, topics, decision/action-item progression, answer latency."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT speaker_id,
                   max(speaker_display)                                   AS speaker_display,
                   count(*)                                               AS lines,
                   sum(word_count)                                        AS words,
                   round(100.0 * sum(word_count) / NULLIF(sum(sum(word_count)) OVER (), 0), 1) AS talk_share_pct,
                   min(ts)                                                AS first_spoke,
                   max(ts)                                                AS last_spoke
            FROM koov_transcript_chunks
            WHERE meeting_id = %s
            GROUP BY speaker_id
            ORDER BY words DESC
            """,
            (meeting_id,),
        )
        speakers = _rows(cur)

        cur.execute(
            """
            SELECT time_bucket(%s::interval, ts) AS bucket,
                   speaker_id,
                   count(*)        AS lines,
                   sum(word_count) AS words
            FROM koov_transcript_chunks
            WHERE meeting_id = %s
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            (bucket, meeting_id),
        )
        timeline = _rows(cur)

        cur.execute(
            """
            SELECT topic, count(*) AS lines, sum(word_count) AS words,
                   min(ts) AS first_mentioned, max(ts) AS last_mentioned
            FROM koov_transcript_chunks
            WHERE meeting_id = %s
            GROUP BY topic
            ORDER BY words DESC
            """,
            (meeting_id,),
        )
        topics = _rows(cur)

        cur.execute(
            """
            SELECT time_bucket(%s::interval, ts)  AS bucket,
                   last(n_decisions, ts)          AS decisions,
                   last(n_action_items, ts)       AS action_items,
                   last(n_open_questions, ts)     AS open_questions
            FROM koov_meeting_snapshots
            WHERE meeting_id = %s
            GROUP BY 1
            ORDER BY 1
            """,
            (bucket, meeting_id),
        )
        progression = _rows(cur)

        cur.execute(
            "SELECT snapshot FROM koov_meeting_snapshots WHERE meeting_id = %s ORDER BY ts DESC LIMIT 1",
            (meeting_id,),
        )
        row = cur.fetchone()
        latest_snapshot = row[0] if row else None

        latency = _latency_stats(cur, "meeting_id = %s", (meeting_id,))

        cur.execute(
            "SELECT min(ts) AS started, max(ts) AS ended, count(*) AS lines, coalesce(sum(word_count),0) AS words "
            "FROM koov_transcript_chunks WHERE meeting_id = %s",
            (meeting_id,),
        )
        totals = _rows(cur)[0]

    return {
        "meeting_id": meeting_id,
        "bucket": bucket,
        "totals": totals,
        "speakers": speakers,
        "timeline": timeline,
        "topics": topics,
        "progression": progression,
        "latest_snapshot": latest_snapshot,
        "answer_latency_ms": latency,
    }


def _latency_stats(cur, where: str, params: tuple) -> dict[str, Any]:
    """p50/p95 of wake->answer latency. Uses Toolkit's percentile_agg when available."""
    base = f"FROM koov_events WHERE kind = 'koov_response' AND value_ms IS NOT NULL AND {where}"
    if _has_toolkit(cur):
        cur.execute(
            f"""
            SELECT count(*) AS answers,
                   approx_percentile(0.5,  percentile_agg(value_ms)) AS p50,
                   approx_percentile(0.95, percentile_agg(value_ms)) AS p95,
                   average(stats_agg(value_ms))                      AS mean
            {base}
            """,
            params,
        )
        source = "timescaledb_toolkit"
    else:
        cur.execute(
            f"""
            SELECT count(*) AS answers,
                   percentile_cont(0.5)  WITHIN GROUP (ORDER BY value_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY value_ms) AS p95,
                   avg(value_ms) AS mean
            {base}
            """,
            params,
        )
        source = "postgres"
    stats = _rows(cur)[0]
    stats["computed_with"] = source
    return stats


def overview(days: int = 7) -> dict[str, Any]:
    """Cross-meeting dashboard: meetings and words per day, busiest speakers, answer latency."""
    since = f"{max(1, int(days))} days"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT time_bucket(INTERVAL '1 day', bucket) AS day,
                   count(DISTINCT meeting_id)            AS meetings,
                   sum(words)                            AS words,
                   sum(lines)                            AS lines
            FROM koov_speaker_activity_1m
            WHERE bucket > now() - %s::interval
            GROUP BY 1
            ORDER BY 1
            """,
            (since,),
        )
        per_day = _rows(cur)

        cur.execute(
            """
            SELECT speaker_id, sum(words) AS words, count(DISTINCT meeting_id) AS meetings
            FROM koov_speaker_activity_1m
            WHERE bucket > now() - %s::interval
            GROUP BY speaker_id
            ORDER BY words DESC
            LIMIT 10
            """,
            (since,),
        )
        top_speakers = _rows(cur)

        cur.execute(
            """
            SELECT meeting_id, min(ts) AS started, max(ts) AS ended,
                   count(*) AS lines, sum(word_count) AS words,
                   count(DISTINCT speaker_id) AS speakers
            FROM koov_transcript_chunks
            WHERE ts > now() - %s::interval
            GROUP BY meeting_id
            ORDER BY started DESC
            LIMIT 50
            """,
            (since,),
        )
        meetings = _rows(cur)

        latency = _latency_stats(cur, "ts > now() - %s::interval", (since,))

    return {"days": days, "per_day": per_day, "top_speakers": top_speakers, "meetings": meetings,
            "answer_latency_ms": latency}


def search(query: str, meeting_id: Optional[str] = None, days: int = 30, limit: int = 50) -> list[dict[str, Any]]:
    """Full-text search across every meeting's transcript (e.g. "what did we say about pricing?")."""
    params: list[Any] = [query, f"{max(1, int(days))} days"]
    extra = ""
    if meeting_id:
        extra = "AND meeting_id = %s"
        params.append(meeting_id)
    params.append(max(1, min(int(limit), 200)))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ts, meeting_id, speaker_id, speaker_display, topic, text,
                   ts_rank(to_tsvector('simple', text), q) AS rank
            FROM koov_transcript_chunks, websearch_to_tsquery('simple', %s) AS q
            WHERE to_tsvector('simple', text) @@ q
              AND ts > now() - %s::interval
              {extra}
            ORDER BY rank DESC, ts DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return _rows(cur)


def health() -> dict[str, Any]:
    info: dict[str, Any] = {"enabled": is_enabled(), "configured": database_url() is not None}
    if not info["enabled"]:
        return info
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            r = cur.fetchone()
            info["timescaledb_version"] = r[0] if r else None
            info["toolkit"] = _has_toolkit(cur)
            cur.execute("SELECT to_regclass('koov_transcript_chunks') IS NOT NULL")
            info["schema_ready"] = bool(cur.fetchone()[0])
            if info["schema_ready"]:
                cur.execute("SELECT count(*), count(DISTINCT meeting_id) FROM koov_transcript_chunks")
                lines, meetings = cur.fetchone()
                info["stored_lines"] = lines
                info["stored_meetings"] = meetings
        info["connected"] = True
    except Exception as exc:
        info["connected"] = False
        info["error"] = str(exc)
    if _sink is not None:
        info["writer"] = _sink.status()
    return info
