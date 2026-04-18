"""
Mapping from Steam app_id to human-readable game title.

Canonical place to name the games. Both routes and the frontend can use this
(frontend fetches via the /games endpoint, see backend/routes/games.py).
"""


APP_NAMES: dict[str, str] = {
    "1272080": "PAYDAY 3",
    "1295660": "Civilization VII",
    "1716740": "Starfield",
    "2246340": "Monster Hunter Wilds",
    "2694490": "Path of Exile 2",
}


def game_name(app_id: str) -> str:
    """Return the human-readable title, falling back to the raw id if unknown."""
    return APP_NAMES.get(str(app_id), str(app_id))
