"""
pipeline/steam_app_index.py — Steam app-name → app_id lookup.

Backs the CM planner's `lookup_app_by_name` tool. Uses Steam's storefront
search endpoint (https://store.steampowered.com/api/storesearch/) which does
server-side ranked fuzzy matching — no API key, no need to download the full
~200K app catalog locally.

Public API:
    search_apps(name, k=5) -> list[dict]   # top-k {app_id, name, type} matches
    has_exact_match(name) -> bool          # case-insensitive exact-name check

Why storefront search over the (now-defunct) ISteamApps/GetAppList endpoint:
    - GetAppList/v2 was retired (returns 404 with "Method 'GetAppList' not
      found in interface 'ISteamApps'"), and the replacement IStoreService/
      GetAppList/v1 requires an authenticated key.
    - The storefront search is what Steam's own UI calls — public, no key,
      tiny payload per query, and Steam's search ranking handles typos and
      partial matches better than local difflib would.

Light per-process LRU cache because the planner may call lookup_app_by_name
multiple times with the same query during one CM run (e.g. probe → confirm).
"""

from __future__ import annotations

import logging
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
DEFAULT_TIMEOUT_SECONDS = 10


@lru_cache(maxsize=128)
def _search_raw(name: str) -> tuple[dict, ...]:
    """Hit Steam storefront search. Returns the raw `items` list as a tuple
    (tuple so it's hashable for lru_cache). Network failures raise; caller
    should wrap and convert to a tool-result error.
    """
    response = requests.get(
        STEAM_STORE_SEARCH_URL,
        params={"term": name, "l": "english", "cc": "US"},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    items = data.get("items", [])
    return tuple(items)


def search_apps(name: str, k: int = 5) -> list[dict]:
    """Return up to k {app_id, name, type} matches for a query name.

    Steam's storefront ranks results by relevance + popularity; the first
    result is usually the canonical match for a popular game. `type` is
    typically "app" for games but may also be "dlc", "music", "demo" — the
    planner SKILL teaches it to prefer type="app".
    """
    if not name or not name.strip():
        return []
    try:
        items = _search_raw(name.strip())
    except requests.RequestException as e:
        logger.warning("Steam storefront search failed for %r: %s", name, e)
        raise

    out: list[dict] = []
    for item in items[:k]:
        out.append({
            "app_id": str(item["id"]),
            "name": item["name"],
            "type": item.get("type", "app"),
        })
    return out


def has_exact_match(name: str) -> bool:
    """True iff the top storefront result has the same name (case-insensitive)."""
    matches = search_apps(name, k=1)
    if not matches:
        return False
    return matches[0]["name"].strip().lower() == name.strip().lower()
