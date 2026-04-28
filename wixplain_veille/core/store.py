# -*- coding: utf-8 -*-
"""
Persistance SQLite pour le pipeline veille.

Deux tables :
  - runs   : une ligne par exécution (date, mode, stats agrégées)
  - items  : un item collecté (post/commentaire/article/trend), dédupliqué par hash(url).

La dédup permet de ne pas re-livrer 10× le même thread Reddit semaine après semaine :
chaque item est inséré une seule fois ; si on le re-rencontre, on met juste à jour
ses signaux (score, num_comments) qui ont pu évoluer.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from .models import Item, RunStats, now_iso


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    mode            TEXT NOT NULL DEFAULT 'weekly',
    items_collected INTEGER NOT NULL DEFAULT 0,
    items_kept      INTEGER NOT NULL DEFAULT 0,
    by_source_json  TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash        TEXT NOT NULL UNIQUE,
    url             TEXT NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    title           TEXT,
    body            TEXT,
    section         TEXT,
    author          TEXT,
    score           INTEGER DEFAULT 0,
    num_comments    INTEGER DEFAULT 0,
    created_at      TEXT,
    parent_url      TEXT,
    raw_json        TEXT,
    -- Champs enrichis (Lots ultérieurs)
    pain_score      REAL DEFAULT 0,
    icp_score       REAL DEFAULT 0,
    combined_score  REAL DEFAULT 0,
    cluster_key     TEXT,
    triggers_json   TEXT,
    verbatims_json  TEXT,
    -- Liens
    first_seen_run  INTEGER,
    last_seen_run   INTEGER,
    last_seen_at    TEXT,
    FOREIGN KEY (first_seen_run) REFERENCES runs(id),
    FOREIGN KEY (last_seen_run)  REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_items_source     ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_last_run   ON items(last_seen_run);
CREATE INDEX IF NOT EXISTS idx_items_combined   ON items(combined_score);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Façade SQLite. Threadée non garantie (mono-process suffit pour notre usage)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- Connexion ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._cursor() as cur:
            cur.executescript(SCHEMA_SQL)

    # -- Runs --------------------------------------------------------------

    def start_run(self, mode: str = "weekly") -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
                (now_iso(), mode),
            )
            return int(cur.lastrowid)

    def end_run(self, stats: RunStats) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE runs
                   SET ended_at = ?, items_collected = ?, items_kept = ?,
                       by_source_json = ?, notes = ?
                   WHERE id = ?""",
                (
                    stats.ended_at or now_iso(),
                    stats.items_collected,
                    stats.items_kept,
                    json.dumps(stats.by_source, ensure_ascii=False),
                    stats.notes,
                    stats.run_id,
                ),
            )

    # -- Items -------------------------------------------------------------

    def is_seen(self, url: str) -> bool:
        h = Item(source="", kind="", url=url).url_hash()
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM items WHERE url_hash = ? LIMIT 1", (h,))
            return cur.fetchone() is not None

    def upsert_item(self, item: Item, run_id: int) -> bool:
        """
        Insère l'item s'il est inconnu, sinon met à jour ses signaux dynamiques.
        Retourne True si nouvel item, False si déjà connu (mise à jour seulement).
        """
        h = item.url_hash()
        ts = now_iso()
        with self._cursor() as cur:
            cur.execute("SELECT id FROM items WHERE url_hash = ?", (h,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """INSERT INTO items (
                        url_hash, url, source, kind, title, body, section, author,
                        score, num_comments, created_at, parent_url, raw_json,
                        pain_score, icp_score, combined_score, cluster_key,
                        triggers_json, verbatims_json,
                        first_seen_run, last_seen_run, last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        h, item.url, item.source, item.kind, item.title, item.body,
                        item.section, item.author, item.score, item.num_comments,
                        item.created_at, item.parent_url,
                        json.dumps(item.raw, ensure_ascii=False),
                        item.pain_score, item.icp_score, item.combined_score,
                        item.cluster_key,
                        json.dumps(item.triggers, ensure_ascii=False),
                        json.dumps(item.verbatims, ensure_ascii=False),
                        run_id, run_id, ts,
                    ),
                )
                return True
            cur.execute(
                """UPDATE items
                   SET score = ?, num_comments = ?,
                       last_seen_run = ?, last_seen_at = ?
                   WHERE id = ?""",
                (item.score, item.num_comments, run_id, ts, row["id"]),
            )
            return False

    def items_for_run(self, run_id: int) -> List[Item]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM items
                   WHERE first_seen_run = ? OR last_seen_run = ?
                   ORDER BY score DESC""",
                (run_id, run_id),
            )
            return [self._row_to_item(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> Item:
        return Item(
            source=row["source"],
            kind=row["kind"],
            url=row["url"],
            title=row["title"] or "",
            body=row["body"] or "",
            section=row["section"] or "",
            author=row["author"] or "",
            score=row["score"] or 0,
            num_comments=row["num_comments"] or 0,
            created_at=row["created_at"],
            parent_url=row["parent_url"] or "",
            raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
            pain_score=row["pain_score"] or 0.0,
            icp_score=row["icp_score"] or 0.0,
            combined_score=row["combined_score"] or 0.0,
            cluster_key=row["cluster_key"] or "",
            triggers=json.loads(row["triggers_json"]) if row["triggers_json"] else [],
            verbatims=json.loads(row["verbatims_json"]) if row["verbatims_json"] else [],
        )
