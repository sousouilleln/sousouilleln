# -*- coding: utf-8 -*-
"""
Interface commune à toutes les sources.

Pour ajouter une nouvelle source, il suffit de :
  1. Créer un module dans sources/, sous-classe de Source.
  2. Ajouter une section dans config.yaml + une clé dans sources_enabled.
  3. L'enregistrer dans wixplain_veille.main (registry).

Aucune logique de scoring ou de filtrage ici : une Source ne fait que collecter
des Items bruts et les retourner. Le filtrage vit dans processing/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from ..core.models import Item


class Source(ABC):
    """Une source de données (Reddit, Google News, RSS, ...)."""

    name: str = "abstract"

    def __init__(self, config: Dict[str, Any]) -> None:
        # `config` est la sous-section du fichier yaml propre à cette source.
        self.config = config

    @abstractmethod
    def fetch(self) -> List[Item]:
        """
        Collecte la matière première et la retourne sous forme d'Items.
        Doit être robuste : une exception ne doit pas faire planter le pipeline,
        chaque source attrape ses propres erreurs et logue.
        """
        raise NotImplementedError
