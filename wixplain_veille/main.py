# -*- coding: utf-8 -*-
"""
Orchestrateur du pipeline veille WiXplain.

Lot 1 : collecte → store SQLite → dump JSON brut.
       Pas encore de scoring, pas encore de digest Markdown
       (ces étapes arriveront avec les Lots 3-5).

Usage :
    python -m wixplain_veille.main --dry-run
        Lance la collecte (Reddit + Google News + Google Trends), persiste
        en SQLite, et écrit un JSON brut horodaté dans data/digests/.

    python -m wixplain_veille.main
        Idem (au Lot 1, --dry-run et le mode normal sont équivalents ;
        le mode normal génèrera le digest Markdown à partir du Lot 5).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

from .core.models import Item, RunStats, now_iso
from .core.store import Store
from .sources.base import Source
from .sources.reddit import RedditSource
from .sources.google_news import GoogleNewsSource
from .sources.google_trends import GoogleTrendsSource


# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"


# ---------------------------------------------------------------------------
# Registry des sources : ajouter une source ici la rend disponible.
# La clé doit matcher la clé dans config.sources_enabled et la section config.
# ---------------------------------------------------------------------------

SOURCE_REGISTRY: Dict[str, type] = {
    "reddit": RedditSource,
    "google_news": GoogleNewsSource,
    "google_trends": GoogleTrendsSource,
    # Lot 2 : "hackernews", "indiehackers", "rss"
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config absent : {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_sources(cfg: Dict[str, Any]) -> List[Source]:
    enabled = cfg.get("sources_enabled", {})
    sources: List[Source] = []
    for key, cls in SOURCE_REGISTRY.items():
        if not enabled.get(key, False):
            continue
        section = cfg.get(key, {}) or {}
        try:
            sources.append(cls(section))
        except Exception as e:
            logging.error("Init source %s impossible : %s", key, e)
    return sources


def resolve_paths(cfg: Dict[str, Any]) -> Dict[str, Path]:
    paths_cfg = cfg.get("paths", {})
    return {
        "data_dir": ROOT / paths_cfg.get("data_dir", "data"),
        "db_file": ROOT / paths_cfg.get("db_file", "data/veille.db"),
        "digests_dir": ROOT / paths_cfg.get("digests_dir", "data/digests"),
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(dry_run: bool) -> int:
    log = logging.getLogger("main")

    cfg = load_config(CONFIG_PATH)
    paths = resolve_paths(cfg)
    paths["data_dir"].mkdir(parents=True, exist_ok=True)
    paths["digests_dir"].mkdir(parents=True, exist_ok=True)

    store = Store(paths["db_file"])
    run_id = store.start_run(mode="weekly")
    log.info("Run #%d démarré.", run_id)

    sources = build_sources(cfg)
    log.info("Sources actives : %s", [s.name for s in sources])

    all_items: List[Item] = []
    by_source: Dict[str, int] = {}
    new_count = 0

    for src in sources:
        log.info("--- %s : collecte ---", src.name)
        try:
            items = src.fetch()
        except Exception as e:
            log.exception("Source %s a planté : %s", src.name, e)
            items = []
        by_source[src.name] = len(items)
        all_items.extend(items)
        for it in items:
            is_new = store.upsert_item(it, run_id)
            if is_new:
                new_count += 1

    stats = RunStats(
        run_id=run_id,
        started_at=now_iso(),
        ended_at=now_iso(),
        mode="weekly",
        items_collected=len(all_items),
        items_kept=new_count,
        by_source=by_source,
        notes="dry-run" if dry_run else "",
    )
    store.end_run(stats)

    log.info(
        "Run #%d terminé : %d items collectés (%d nouveaux). Détails : %s",
        run_id, len(all_items), new_count, by_source,
    )

    # Dump JSON brut horodaté.
    out_path = paths["digests_dir"] / f"raw_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json"
    payload = {
        "run": stats.to_dict(),
        "items": [it.to_dict() for it in all_items],
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("Dump JSON écrit : %s", out_path)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WiXplain Veille — pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lot 1 : collecte + dump JSON brut, sans rendu Markdown.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Logs DEBUG.",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    try:
        return run_pipeline(dry_run=args.dry_run)
    except KeyboardInterrupt:
        logging.warning("Interrompu par l'utilisateur.")
        return 130
    except Exception as e:
        logging.exception("Erreur fatale : %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
