# -*- coding: utf-8 -*-
"""
Source Google Trends — via pytrends.

On encode chaque "trend" comme un Item kind="trend" :
  - title  = le mot-clé suivi
  - body   = un petit résumé (intérêt moyen, tendance, requêtes associées)
  - score  = intérêt moyen (0-100) — utilisable comme signal pour le ranking
  - raw    = données brutes pour rendu détaillé en aval

Si pytrends n'est pas installé ou que Google Trends est temporairement inaccessible,
on logue et on retourne une liste vide — sans planter le pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from ..core.models import Item, now_iso
from .base import Source


log = logging.getLogger(__name__)


class GoogleTrendsSource(Source):
    name = "google_trends"

    def fetch(self) -> List[Item]:
        try:
            from pytrends.request import TrendReq  # type: ignore
        except ImportError:
            log.warning("pytrends non installé. `pip install pytrends` pour activer.")
            return []

        keywords: List[str] = list(self.config.get("keywords", []))
        if not keywords:
            return []
        timeframe = self.config.get("timeframe", "now 7-d")
        geo_main = self.config.get("geo_main", "FR")
        geo_world = self.config.get("geo_world", "")
        related_for = int(self.config.get("related_queries_for", 2))

        items: List[Item] = []
        try:
            pytrends = TrendReq(hl="fr-FR", tz=60, timeout=(10, 25))
            pytrends.build_payload(keywords[:5], timeframe=timeframe, geo=geo_main)
            df = pytrends.interest_over_time()
        except Exception as e:
            log.warning("Google Trends interest_over_time : %s", e)
            return []

        if df is None or df.empty:
            return []

        for col in df.columns:
            if col == "isPartial":
                continue
            vals = df[col].dropna()
            if len(vals) < 2:
                continue
            avg = float(vals.mean())
            last = float(vals.iloc[-1])
            tendency = "hausse" if last > avg else ("baisse" if last < avg else "stable")
            body_lines = [
                f"Intérêt moyen sur {timeframe} ({geo_main or 'monde'}) : {avg:.0f}/100",
                f"Dernière valeur : {last:.0f}  →  tendance {tendency}",
            ]
            items.append(Item(
                source="google_trends",
                kind="trend",
                url=f"https://trends.google.com/trends/explore?q={col.replace(' ', '+')}&geo={geo_main}",
                title=col,
                body="\n".join(body_lines),
                section=geo_main or "world",
                score=int(avg),
                created_at=now_iso(),
                raw={
                    "avg": avg,
                    "last": last,
                    "tendency": tendency,
                    "timeframe": timeframe,
                    "geo": geo_main,
                },
            ))

        # Requêtes associées — opportunités d'angle pour LinkedIn
        for kw in keywords[:related_for]:
            try:
                pytrends.build_payload([kw], timeframe=timeframe, geo=geo_world)
                rq = pytrends.related_queries()
                top_df = rq.get(kw, {}).get("top") if rq and kw in rq else None
                if top_df is None or top_df.empty:
                    continue
                related_list = top_df.head(8).to_dict("records")
                items.append(Item(
                    source="google_trends",
                    kind="trend",
                    url=f"https://trends.google.com/trends/explore?q={kw.replace(' ', '+')}",
                    title=f"Requêtes associées : {kw}",
                    body="\n".join(
                        f"- {r.get('query', '')} (poids {r.get('value', 0)})" for r in related_list
                    ),
                    section="related",
                    score=0,
                    created_at=now_iso(),
                    raw={"keyword": kw, "related": related_list},
                ))
                time.sleep(1.5)
            except Exception as e:
                log.warning("Google Trends related_queries [%s] : %s", kw, e)

        log.info("Google Trends : %d items collectés.", len(items))
        return items
