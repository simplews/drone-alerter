"""SQLite storage for learning data: targets, point mentions, outcome labels,
raw posts (with a limited retention time) and candidates for new names.

By design, the code does NOT store where and when a target was shot down. The label
``downed_en_route_if`` ("did not arrive") is enough for the model. An archive of shoot-down points is sensitive data.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

OUTCOME_REACHED = "reached_if"           # arrived at IF (has an arrival time)
OUTCOME_DOWNED = "downed_en_route_if"    # went toward IF but did not arrive
OUTCOME_DIVERTED = "diverted"            # went to a different region
OUTCOME_UNKNOWN = "unknown"
OUTCOMES = {OUTCOME_REACHED, OUTCOME_DOWNED, OUTCOME_DIVERTED, OUTCOME_UNKNOWN}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id              TEXT PRIMARY KEY,
    first_seen_utc  TEXT NOT NULL,
    outcome         TEXT,
    arrived_at_utc  TEXT,
    labeled_at_utc  TEXT,
    notes           TEXT
);
CREATE TABLE IF NOT EXISTS mentions (
    target_id      TEXT NOT NULL REFERENCES targets(id),
    point_id       TEXT NOT NULL,
    channel        TEXT NOT NULL,
    message_id     INTEGER NOT NULL DEFAULT 0,
    posted_at_utc  TEXT NOT NULL,
    PRIMARY KEY (target_id, point_id, channel, message_id)
);
CREATE TABLE IF NOT EXISTS posts (
    channel            TEXT NOT NULL,
    message_id         INTEGER NOT NULL,
    posted_at_utc      TEXT NOT NULL,
    text               TEXT NOT NULL,
    is_target_related  INTEGER NOT NULL DEFAULT 0,
    aliases_scanned    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel, message_id)
);
CREATE TABLE IF NOT EXISTS alias_candidates (
    phrase_norm         TEXT PRIMARY KEY,
    surface_forms       TEXT NOT NULL,
    count               INTEGER NOT NULL,
    first_seen_utc      TEXT NOT NULL,
    last_seen_utc       TEXT NOT NULL,
    suggested_point_id  TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    decided_point_id    TEXT,
    new_point_level     INTEGER,
    example             TEXT
);
"""


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("Час має бути з часовою зоною (UTC)")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"Час без часової зони: {value}")
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class ArrivalObservation:
    """One observation for the ETA table: first mention of a point -> arrival."""
    target_id: str
    point_id: str
    first_post_utc: datetime
    minutes_to_arrival: float


@dataclass(frozen=True)
class OutcomeRow:
    target_id: str
    point_id: str
    outcome: str


@dataclass(frozen=True)
class AliasCandidate:
    phrase_norm: str
    surface_forms: list[str]
    count: int
    suggested_point_id: str | None
    status: str
    decided_point_id: str | None
    new_point_level: int | None
    example: str | None


class LearningStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- write events (the tracking engine calls these) ---------------
    def add_post(self, channel: str, message_id: int, posted_at: datetime, text: str,
                 is_target_related: bool) -> None:
        """Store or update a post. An edit of a post with new text
        resets the scan flag, so the new names also go into the analysis."""
        posted = to_utc_iso(posted_at)
        with self.conn:
            row = self.conn.execute(
                "SELECT text FROM posts WHERE channel=? AND message_id=?", (channel, message_id)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO posts(channel, message_id, posted_at_utc, text, is_target_related)"
                    " VALUES (?,?,?,?,?)",
                    (channel, message_id, posted, text, int(is_target_related)),
                )
            elif row["text"] != text:
                self.conn.execute(
                    "UPDATE posts SET text=?, is_target_related=?, aliases_scanned=0"
                    " WHERE channel=? AND message_id=?",
                    (text, int(is_target_related), channel, message_id),
                )

    def add_mention(self, target_id: str, point_id: str, channel: str, posted_at: datetime,
                    message_id: int = 0) -> None:
        posted = to_utc_iso(posted_at)
        with self.conn:
            self.conn.execute(
                "INSERT INTO targets(id, first_seen_utc) VALUES (?, ?)"
                " ON CONFLICT(id) DO UPDATE SET first_seen_utc=MIN(first_seen_utc, excluded.first_seen_utc)",
                (target_id, posted),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO mentions(target_id, point_id, channel, message_id, posted_at_utc)"
                " VALUES (?,?,?,?,?)",
                (target_id, point_id, channel, message_id, posted),
            )

    # --- labeling (a person, once a day) -------------------------------
    def label_target(self, target_id: str, outcome: str, arrived_at: datetime | None = None,
                     notes: str | None = None) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"Невідомий результат {outcome!r}. Допустимі: {sorted(OUTCOMES)}")
        if outcome == OUTCOME_REACHED and arrived_at is None:
            raise ValueError("Для reached_if потрібен час прибуття (arrived_at)")
        if outcome != OUTCOME_REACHED and arrived_at is not None:
            raise ValueError("Час прибуття вказується лише для reached_if")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE targets SET outcome=?, arrived_at_utc=?, labeled_at_utc=?, notes=? WHERE id=?",
                (
                    outcome,
                    to_utc_iso(arrived_at) if arrived_at else None,
                    to_utc_iso(datetime.now(UTC)),
                    notes,
                    target_id,
                ),
            )
            if cur.rowcount == 0:
                raise LookupError(f"Ціль {target_id!r} не знайдена")

    def unlabeled_targets(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.id, t.first_seen_utc, GROUP_CONCAT(DISTINCT m.point_id) AS points"
            " FROM targets t LEFT JOIN mentions m ON m.target_id=t.id"
            " WHERE t.outcome IS NULL GROUP BY t.id ORDER BY t.first_seen_utc"
        ).fetchall()

    def has_open_targets(self, now: datetime, window_hours: float) -> bool:
        """True if unlabeled targets appeared recently (they can still be in flight)."""
        since = to_utc_iso(now - timedelta(hours=window_hours))
        row = self.conn.execute(
            "SELECT 1 FROM targets WHERE outcome IS NULL AND first_seen_utc >= ? LIMIT 1", (since,)
        ).fetchone()
        return row is not None

    def count_labeled(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM targets WHERE outcome IS NOT NULL AND outcome != ?",
            (OUTCOME_UNKNOWN,),
        ).fetchone()[0]

    # --- samples for learning ------------------------------------------
    def arrival_observations(self) -> list[ArrivalObservation]:
        """For each target that arrived and each point: the earliest mention across all channels."""
        rows = self.conn.execute(
            "SELECT m.target_id, m.point_id, MIN(m.posted_at_utc) AS first_post, t.arrived_at_utc"
            " FROM mentions m JOIN targets t ON t.id = m.target_id"
            " WHERE t.outcome = ? AND t.arrived_at_utc IS NOT NULL"
            " GROUP BY m.target_id, m.point_id",
            (OUTCOME_REACHED,),
        ).fetchall()
        result = []
        for r in rows:
            first = parse_utc(r["first_post"])
            minutes = (parse_utc(r["arrived_at_utc"]) - first).total_seconds() / 60
            if minutes < 0:
                continue  # mention after arrival (a summary written later) is not an observation
            result.append(ArrivalObservation(r["target_id"], r["point_id"], first, minutes))
        return result

    def outcome_rows(self) -> list[OutcomeRow]:
        rows = self.conn.execute(
            "SELECT DISTINCT m.target_id, m.point_id, t.outcome FROM mentions m"
            " JOIN targets t ON t.id = m.target_id WHERE t.outcome IS NOT NULL"
        ).fetchall()
        return [OutcomeRow(r["target_id"], r["point_id"], r["outcome"]) for r in rows]

    def target_first_seen(self) -> dict[str, datetime]:
        rows = self.conn.execute("SELECT id, first_seen_utc FROM targets").fetchall()
        return {r["id"]: parse_utc(r["first_seen_utc"]) for r in rows}

    # --- posts for the search of new names -----------------------------
    def unscanned_target_posts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT channel, message_id, posted_at_utc, text FROM posts"
            " WHERE is_target_related=1 AND aliases_scanned=0 ORDER BY posted_at_utc"
        ).fetchall()

    def mark_scanned(self, channel: str, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE posts SET aliases_scanned=1 WHERE channel=? AND message_id=?",
                (channel, message_id),
            )

    def purge_posts(self, older_than_days: int, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        cutoff = to_utc_iso(now - timedelta(days=older_than_days))
        with self.conn:
            return self.conn.execute("DELETE FROM posts WHERE posted_at_utc < ?", (cutoff,)).rowcount

    # --- name candidates -----------------------------------------------
    def upsert_alias_candidate(self, phrase_norm: str, surface: str, seen_at: str,
                               suggested_point_id: str | None, example: str) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT surface_forms FROM alias_candidates WHERE phrase_norm=?", (phrase_norm,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO alias_candidates(phrase_norm, surface_forms, count, first_seen_utc,"
                    " last_seen_utc, suggested_point_id, example) VALUES (?,?,?,?,?,?,?)",
                    (phrase_norm, json.dumps([surface], ensure_ascii=False), 1, seen_at, seen_at,
                     suggested_point_id, example[:300]),
                )
                return
            forms = json.loads(row["surface_forms"])
            if surface not in forms:
                forms.append(surface)
            self.conn.execute(
                "UPDATE alias_candidates SET surface_forms=?, count=count+1, last_seen_utc=MAX(last_seen_utc, ?)"
                " WHERE phrase_norm=?",
                (json.dumps(forms, ensure_ascii=False), seen_at, phrase_norm),
            )

    def alias_candidates(self, status: str | None = None, min_count: int = 1) -> list[AliasCandidate]:
        sql = "SELECT * FROM alias_candidates WHERE count >= ?"
        params: list = [min_count]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY count DESC, last_seen_utc DESC"
        return [
            AliasCandidate(
                phrase_norm=r["phrase_norm"],
                surface_forms=json.loads(r["surface_forms"]),
                count=r["count"],
                suggested_point_id=r["suggested_point_id"],
                status=r["status"],
                decided_point_id=r["decided_point_id"],
                new_point_level=r["new_point_level"],
                example=r["example"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def decide_alias(self, phrase_norm: str, status: str, point_id: str | None = None,
                     new_point_level: int | None = None) -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("status має бути approved або rejected")
        if status == "approved" and not point_id:
            raise ValueError("Для схвалення потрібен id пункту")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE alias_candidates SET status=?, decided_point_id=?, new_point_level=?"
                " WHERE phrase_norm=?",
                (status, point_id, new_point_level, phrase_norm),
            )
            if cur.rowcount == 0:
                raise LookupError(f"Кандидата {phrase_norm!r} не знайдено")
