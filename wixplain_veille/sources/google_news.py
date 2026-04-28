# -*- coding: utf-8 -*-
"""
Source Google News — RSS public, sans authentification.

On interroge plusieurs locales (FR + EN par défaut) et plusieurs requêtes,
on dédupe par URL canonique. Chaque article devient un Item kind="article".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List
from urllib.parse import quote_plus

import feedparser

from ..core.models import Item
from .base import Source


log = logging.getLogger(__name__)


class GoogleNewsSource(Source):
    name = "google_news"

    BASE = "https://news.google.com/rss/search"

    def fetch(self) -> List[Item]:
        locales = self.config.get("locales", [{"hl": "fr", "gl": "FR", "ceid": "FR:fr"}])
        results_per_query = int(self.config.get("results_per_query", 8))
        queries: List[str] = list(self.config.get("queries", []))

        items: List[Item] = []
        seen: set[str] = set()

        for q in queries:
            for loc in locales:
                url = self._build_url(q, loc)
                try:
                    feed = feedparser.parse(url)
                except Exception as e:
                    log.warning("Google News parse [%s/%s] : %s", q, loc.get("hl"), e)
                    continue

                for entry in feed.entries[:results_per_query]:
                    link = entry.get("link", "") or ""
                    if not link or link in seen:
                        continue
                    seen.add(link)
                    title = entry.get("title", "") or ""
                    src_obj = entry.get("source", {}) or {}
                    src_name = src_obj.get("title", "") if isinstance(src_obj, dict) else ""
                    items.append(Item(
                        source="google_news",
                        kind="article",
                        url=link,
                        title=title,
                        body=entry.get("summary", "") or "",
                        section=loc.get("ceid", ""),
                        author=src_name,
                        score=0,
                        num_comments=0,
                        created_at=entry.get("published", "") or None,
                        raw={"query": q, "locale": loc, "source_name": src_name},
                    ))
                # Politesse minimale entre requêtes (RSS Google reste tolérant).
                time.sleep(0.4)

        log.info("Google News : %d articles collectés.", len(items))
        return items

    def _build_url(self, query: str, locale: Dict[str, Any]) -> str:
        q_enc = quote_plus(query)
        hl = locale.get("hl", "fr")
        gl = locale.get("gl", "FR")
        ceid = locale.get("ceid", "FR:fr")
        return f"{self.BASE}?q={q_enc}&hl={hl}&gl={gl}&ceid={ceid}"
