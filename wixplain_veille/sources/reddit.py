# -*- coding: utf-8 -*-
"""
Source Reddit — collecte posts + commentaires.

Deux backends derrière une interface unique :
  - PrawBackend : utilise PRAW (rate-limit confortable, scraping commentaires propre)
                  s'active si REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET sont fournis dans .env
                  ET que praw est installé ET que l'auth fonctionne.
  - JsonBackend : utilise l'API JSON publique de Reddit (sans auth).
                  Toujours dispo, fallback automatique. Rate-limit plus strict mais ok
                  pour un run hebdo de quelques centaines d'items.

Stratégie de collecte (les deux backends) :
  1. Pour chaque subreddit : on récupère le top de la période ("/r/X/top?t=week").
  2. Pour chaque mot-clé de tête (config.top_keywords_for_search) : on cherche
     en site-wide ("/search?q=KW&t=week") — capte les posts qui sortent de notre
     liste de subs mais matchent un terme ICP.
  3. Dédup par URL.
  4. Pour les posts dépassant les seuils min_score/min_comments : on récupère
     les top-N commentaires (profondeur 1 uniquement, triés par score).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from ..core.models import Item, epoch_to_iso
from .base import Source


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend abstrait
# ---------------------------------------------------------------------------

class _Backend:
    name = "abstract"

    def search_subreddit_top(self, sub: str, period: str, limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def search_global(self, query: str, period: str, limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def fetch_top_comments(self, post_id: str, sub: str, limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Backend PRAW
# ---------------------------------------------------------------------------

class _PrawBackend(_Backend):
    name = "praw"

    def __init__(self, client_id: str, client_secret: str, user_agent: str):
        import praw  # type: ignore  # noqa: F401  (import tardif pour éviter import si non utilisé)
        self._reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
            check_for_async=False,
        )
        # Force read-only (pas de username/password fourni).
        self._reddit.read_only = True

    def _post_to_dict(self, p: Any, sub_hint: str = "") -> Dict[str, Any]:
        return {
            "id": p.id,
            "title": p.title or "",
            "selftext": p.selftext or "",
            "author": str(p.author) if p.author else "",
            "score": int(p.score or 0),
            "num_comments": int(p.num_comments or 0),
            "created_utc": float(p.created_utc) if p.created_utc else None,
            "subreddit": str(p.subreddit) if p.subreddit else sub_hint,
            "permalink": p.permalink,
            "url_full": f"https://reddit.com{p.permalink}",
        }

    def search_subreddit_top(self, sub: str, period: str, limit: int) -> List[Dict[str, Any]]:
        out = []
        for p in self._reddit.subreddit(sub).top(time_filter=period, limit=limit):
            out.append(self._post_to_dict(p, sub))
        return out

    def search_global(self, query: str, period: str, limit: int) -> List[Dict[str, Any]]:
        out = []
        # Recherche site-wide via "all"
        for p in self._reddit.subreddit("all").search(query, sort="top", time_filter=period, limit=limit):
            out.append(self._post_to_dict(p))
        return out

    def fetch_top_comments(self, post_id: str, sub: str, limit: int) -> List[Dict[str, Any]]:
        submission = self._reddit.submission(id=post_id)
        try:
            submission.comments.replace_more(limit=0)
        except Exception as e:
            log.warning("praw replace_more failed for %s : %s", post_id, e)
        top_level = list(submission.comments)
        top_level.sort(key=lambda c: int(getattr(c, "score", 0) or 0), reverse=True)
        out = []
        for c in top_level[:limit]:
            out.append({
                "id": getattr(c, "id", ""),
                "body": getattr(c, "body", "") or "",
                "score": int(getattr(c, "score", 0) or 0),
                "author": str(getattr(c, "author", "")) if getattr(c, "author", None) else "",
                "created_utc": float(getattr(c, "created_utc", 0) or 0),
                "permalink": getattr(c, "permalink", ""),
                "url_full": f"https://reddit.com{getattr(c, 'permalink', '')}",
                "post_id": post_id,
                "subreddit": sub,
            })
        return out


# ---------------------------------------------------------------------------
# Backend JSON public
# ---------------------------------------------------------------------------

class _JsonBackend(_Backend):
    name = "json_public"
    BASE = "https://www.reddit.com"

    def __init__(self, user_agent: str, request_delay: float):
        self.headers = {"User-Agent": user_agent}
        self.delay = request_delay

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        time.sleep(self.delay)
        url = f"{self.BASE}{path}"
        r = requests.get(url, headers=self.headers, params=params, timeout=15)
        if r.status_code == 429:
            log.warning("Reddit JSON 429 sur %s — pause 30s", url)
            time.sleep(30)
            r = requests.get(url, headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _post_to_dict(d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": d.get("id", ""),
            "title": d.get("title", "") or "",
            "selftext": d.get("selftext", "") or "",
            "author": d.get("author", "") or "",
            "score": int(d.get("score", 0) or 0),
            "num_comments": int(d.get("num_comments", 0) or 0),
            "created_utc": float(d.get("created_utc", 0) or 0),
            "subreddit": d.get("subreddit", "") or "",
            "permalink": d.get("permalink", "") or "",
            "url_full": f"https://reddit.com{d.get('permalink', '')}",
        }

    def search_subreddit_top(self, sub: str, period: str, limit: int) -> List[Dict[str, Any]]:
        try:
            data = self._get(f"/r/{sub}/top.json", {"t": period, "limit": limit})
            children = data.get("data", {}).get("children", [])
            return [self._post_to_dict(c["data"]) for c in children]
        except Exception as e:
            log.warning("Reddit JSON top r/%s : %s", sub, e)
            return []

    def search_global(self, query: str, period: str, limit: int) -> List[Dict[str, Any]]:
        try:
            data = self._get(
                "/search.json",
                {"q": query, "sort": "top", "t": period, "limit": limit, "type": "link"},
            )
            children = data.get("data", {}).get("children", [])
            return [self._post_to_dict(c["data"]) for c in children]
        except Exception as e:
            log.warning("Reddit JSON search [%s] : %s", query, e)
            return []

    def fetch_top_comments(self, post_id: str, sub: str, limit: int) -> List[Dict[str, Any]]:
        try:
            data = self._get(
                f"/r/{sub}/comments/{post_id}.json",
                {"limit": limit, "depth": 1, "sort": "top"},
            )
            # Le endpoint renvoie une liste de 2 listings : [post, comments]
            if not isinstance(data, list) or len(data) < 2:
                return []
            children = data[1].get("data", {}).get("children", [])
            out = []
            for c in children[:limit]:
                if c.get("kind") != "t1":
                    continue
                d = c.get("data", {})
                out.append({
                    "id": d.get("id", ""),
                    "body": d.get("body", "") or "",
                    "score": int(d.get("score", 0) or 0),
                    "author": d.get("author", "") or "",
                    "created_utc": float(d.get("created_utc", 0) or 0),
                    "permalink": d.get("permalink", "") or "",
                    "url_full": f"https://reddit.com{d.get('permalink', '')}",
                    "post_id": post_id,
                    "subreddit": sub,
                })
            return out
        except Exception as e:
            log.warning("Reddit JSON comments %s/%s : %s", sub, post_id, e)
            return []


# ---------------------------------------------------------------------------
# Source publique
# ---------------------------------------------------------------------------

class RedditSource(Source):
    name = "reddit"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.user_agent = os.environ.get(
            "REDDIT_USER_AGENT",
            "WiXplain-VeilleBot/1.0 (contact: senach.moy@gmail.com)",
        )
        self.request_delay = float(config.get("request_delay_seconds", 1.5))
        self._backend: _Backend = self._init_backend()

    def _init_backend(self) -> _Backend:
        cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
        csec = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
        if cid and csec:
            try:
                backend = _PrawBackend(cid, csec, self.user_agent)
                # Sanity-check : un appel simple pour valider l'auth.
                _ = list(backend._reddit.subreddit("test").top(time_filter="day", limit=1))
                log.info("Reddit : backend PRAW activé (auth OK).")
                return backend
            except ImportError:
                log.warning("praw n'est pas installé — bascule sur JSON public.")
            except Exception as e:
                log.warning("Auth PRAW échouée (%s) — bascule sur JSON public.", e)
        else:
            log.info("REDDIT_CLIENT_ID/SECRET absents — utilisation JSON public.")
        return _JsonBackend(self.user_agent, self.request_delay)

    # -- Conversion vers Item ---------------------------------------------

    @staticmethod
    def _post_to_item(d: Dict[str, Any]) -> Item:
        return Item(
            source="reddit",
            kind="post",
            url=d["url_full"],
            title=d["title"],
            body=d.get("selftext", ""),
            section=d.get("subreddit", ""),
            author=d.get("author", ""),
            score=d.get("score", 0),
            num_comments=d.get("num_comments", 0),
            created_at=epoch_to_iso(d.get("created_utc")),
            raw={"reddit_post_id": d.get("id", "")},
        )

    @staticmethod
    def _comment_to_item(d: Dict[str, Any], parent_url: str) -> Item:
        return Item(
            source="reddit",
            kind="comment",
            url=d["url_full"],
            title="",
            body=d.get("body", ""),
            section=d.get("subreddit", ""),
            author=d.get("author", ""),
            score=d.get("score", 0),
            num_comments=0,
            created_at=epoch_to_iso(d.get("created_utc")),
            parent_url=parent_url,
            raw={"reddit_comment_id": d.get("id", ""), "reddit_post_id": d.get("post_id", "")},
        )

    # -- Pipeline de collecte ---------------------------------------------

    def fetch(self) -> List[Item]:
        period = self.config.get("period", "week")
        posts_per_query = int(self.config.get("posts_per_query", 25))
        max_per_sub = int(self.config.get("max_posts_per_subreddit", 40))
        top_kw_count = int(self.config.get("top_keywords_for_search", 8))

        fetch_comments = bool(self.config.get("fetch_comments", True))
        comments_per_post = int(self.config.get("comments_per_post", 8))
        min_score = int(self.config.get("comment_fetch_min_score", 3))
        min_comments = int(self.config.get("comment_fetch_min_comments", 2))

        subs: List[str] = list(self.config.get("subreddits", []))
        keywords: List[str] = list(self.config.get("keywords", []))[:top_kw_count]

        seen_post_urls: set[str] = set()
        post_dicts: List[Dict[str, Any]] = []

        # Étape 1 : top-of-period par subreddit
        log.info("Reddit/%s : top-week sur %d subreddits…", self._backend.name, len(subs))
        for sub in subs:
            posts = self._backend.search_subreddit_top(sub, period, posts_per_query)
            kept = 0
            for p in posts:
                u = p.get("url_full", "")
                if not u or u in seen_post_urls:
                    continue
                seen_post_urls.add(u)
                post_dicts.append(p)
                kept += 1
                if kept >= max_per_sub:
                    break

        # Étape 2 : recherches site-wide sur les top mots-clés
        log.info("Reddit/%s : recherche site-wide sur %d mots-clés…", self._backend.name, len(keywords))
        for kw in keywords:
            posts = self._backend.search_global(kw, period, posts_per_query)
            for p in posts:
                u = p.get("url_full", "")
                if not u or u in seen_post_urls:
                    continue
                seen_post_urls.add(u)
                post_dicts.append(p)

        log.info("Reddit : %d posts uniques collectés.", len(post_dicts))

        # Étape 3 : conversion en Items + collecte des commentaires
        items: List[Item] = []
        comment_count = 0
        for p in post_dicts:
            post_item = self._post_to_item(p)
            items.append(post_item)

            if not fetch_comments:
                continue
            if post_item.score < min_score and post_item.num_comments < min_comments:
                continue

            comments = self._backend.fetch_top_comments(
                post_id=p.get("id", ""),
                sub=p.get("subreddit", ""),
                limit=comments_per_post,
            )
            for c in comments:
                items.append(self._comment_to_item(c, post_item.url))
                comment_count += 1

        log.info("Reddit : %d commentaires collectés.", comment_count)
        return items
