# -*- coding: utf-8 -*-
"""
Modèles de données partagés par tout le pipeline.

Un `Item` est l'unité atomique : un post Reddit, un commentaire Reddit,
un article Google News, une trend, plus tard un post HN ou un article RSS.
Toutes les sources produisent des `Item`s ; tous les modules de processing
travaillent sur des `Item`s.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional
import hashlib


# ---------------------------------------------------------------------------
# Item : unité atomique collectée
# ---------------------------------------------------------------------------

@dataclass
class Item:
    # --- Identification ---
    source: str                    # "reddit", "google_news", "google_trends", "hackernews", ...
    kind: str                      # "post", "comment", "article", "trend"
    url: str                       # URL canonique (sert de clé de dédup)

    # --- Contenu textuel ---
    title: str = ""
    body: str = ""                 # corps : selftext Reddit, snippet article, etc.

    # --- Métadonnées contextuelles ---
    section: str = ""              # subreddit, locale Google News, catégorie RSS...
    author: str = ""

    # --- Signaux quantitatifs ---
    score: int = 0                 # upvotes Reddit, points HN, 0 sinon
    num_comments: int = 0
    created_at: Optional[str] = None  # ISO 8601 UTC ; None si inconnu

    # --- Données brutes (pour debug et futur enrichissement) ---
    raw: Dict[str, Any] = field(default_factory=dict)

    # --- Champs remplis par le pipeline (Lots ultérieurs) ---
    pain_score: float = 0.0
    icp_score: float = 0.0
    combined_score: float = 0.0
    cluster_key: str = ""
    triggers: List[str] = field(default_factory=list)   # ex: ["fundraising_fr", "new_tool"]
    verbatims: List[str] = field(default_factory=list)

    # --- Lien parent pour les commentaires ---
    parent_url: str = ""

    def url_hash(self) -> str:
        """Clé de dédup stable, même si l'URL contient des variations mineures."""
        normalized = self.url.strip().lower().rstrip("/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# RunStats : métadonnées d'une exécution complète
# ---------------------------------------------------------------------------

@dataclass
class RunStats:
    run_id: int
    started_at: str
    ended_at: Optional[str] = None
    mode: str = "weekly"
    items_collected: int = 0
    items_kept: int = 0
    by_source: Dict[str, int] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers temps
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Datetime UTC au format ISO 8601 (sans microsecondes)."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def epoch_to_iso(epoch: Optional[float]) -> Optional[str]:
    """Convertit un timestamp Unix en ISO 8601 UTC. None si invalide."""
    if epoch is None:
        return None
    try:
        return datetime.utcfromtimestamp(float(epoch)).replace(microsecond=0).isoformat() + "Z"
    except (TypeError, ValueError, OSError):
        return None
