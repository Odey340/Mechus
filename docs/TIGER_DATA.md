# Tiger Data (TimescaleDB) integration

Mechus stores every meeting in **Tiger Data** (Tiger Cloud: Postgres + TimescaleDB). Each transcript line, decision/action-item snapshot and answer-latency measurement becomes time-series data you can query with standard SQL.

The local `mechus_meetings/` store still powers live retrieval. Tiger Data is a **mirror** written on a background thread, so if the database is slow or down, Mechus keeps talking.

## Setup (about 5 minutes)

1. Create a free service: [Tiger Data quickstart](https://mlh.link/tigerdata-quickstart). Download the connection file it gives you, because the password is only shown once.
2. Install the driver (already in `requirements.txt`): `pip install "psycopg[binary,pool]"`
3. Add the URL to `.env.local` (gitignored, loaded after `.env`):
   ```
   TIGER_DATABASE_URL=postgres://tsdbadmin:<PASSWORD>@<HOST>:<PORT>/tsdb?sslmode=require
   ```
4. Create the schema and load a demo meeting:
   ```
   python scripts/tiger_init.py --demo
   ```
   Mechus also creates the schema automatically on its first write.

## What gets stored

| Object | Type | Contents |
|---|---|---|
| `mechus_transcript_chunks` | hypertable (segmented by `meeting_id`) | `ts, meeting_id, speaker_id, speaker_display, text, word_count, chunk_id, topic, tags, content_hash` |
| `mechus_meeting_snapshots` | hypertable | decision / action-item / open-question counts over time, plus the full JSON snapshot |
| `mechus_events` | hypertable (segmented by `kind`) | `mechus_response` = wake-to-answer latency in ms, with the intent and wake source |
| `mechus_speaker_activity_1m` | continuous aggregate (real-time) | words and lines per speaker per minute |

Every hypertable gets a columnstore (compression) policy after 7 days, so long meeting histories take far less space. Set `MECHUS_TIGER_RETENTION_DAYS=90` to drop raw transcript lines after N days.

When available, the [TimescaleDB Toolkit](https://mlh.link/tigerdata-toolkit) is enabled automatically. Latency stats then use `percentile_agg` / `approx_percentile` / `stats_agg`, with a plain Postgres fallback.

## API

All routes respect `MECHUS_API_KEY`.

| Route | Returns |
|---|---|
| `GET /api/tiger/status` | connection, TimescaleDB version, toolkit, row counts, writer health |
| `GET /api/meetings/<id>/analytics?bucket=1 minute` | talk share per speaker, activity timeline (`time_bucket`), topics, decisions/action items over time (`last()`), answer latency p50/p95 |
| `GET /api/analytics/overview?days=7` | meetings and words per day, top speakers, latency (from the continuous aggregate) |
| `GET /api/tiger/search?q=pricing&days=30` | full-text search across every meeting transcript |

## Example SQL (Tiger Console → SQL editor)

```sql
-- Who dominated the meeting?
SELECT speaker_id, sum(word_count) AS words,
       round(100.0 * sum(word_count) / sum(sum(word_count)) OVER (), 1) AS share_pct
FROM mechus_transcript_chunks WHERE meeting_id = 'MEETING_ID'
GROUP BY speaker_id ORDER BY words DESC;

-- Conversation energy per 5 minutes
SELECT time_bucket('5 minutes', ts) AS t, sum(word_count) AS words
FROM mechus_transcript_chunks WHERE meeting_id = 'MEETING_ID' GROUP BY t ORDER BY t;

-- How fast does Mechus answer (Toolkit)?
SELECT approx_percentile(0.95, percentile_agg(value_ms)) AS p95_ms
FROM mechus_events WHERE kind = 'mechus_response' AND ts > now() - INTERVAL '1 day';
```

More ideas: [Tiger Data tutorials](https://mlh.link/tigerdata-tutorials).
