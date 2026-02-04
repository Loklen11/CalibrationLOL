"""
Client minimal pour l'API Polymarket (Gamma + CLOB).
Utilisé par le script paper Polymarket LoL pour récupérer event + prix (best ask).
Prix temps réel : WebSocket Market Channel (best_bid / best_ask en direct).
Aucune dépendance à predator_paper_trading.
"""

import json
import logging
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
WSS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Client WebSocket singleton (initialisé au démarrage FastAPI, None en script standalone).
_ws_client: "PolymarketWebSocketClient | None" = None


def get_token_ids_from_lol_events(limit: int = 50) -> list[str]:
    """
    Récupère tous les token_ids des marchés LoL (pour abonnement WebSocket initial).
    Utilisé au démarrage FastAPI pour s'abonner aux prix temps réel.
    """
    events = get_events_lol(limit=limit, upcoming_only=True, main_leagues_only=False)
    seen: set[str] = set()
    out: list[str] = []
    for ev in events:
        markets = ev.get("markets") or []
        market = _select_winner_market(markets)
        if not market:
            continue
        token_ids, _ = parse_market_tokens(market)
        for tid in token_ids:
            if tid and tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


def get_ws_client() -> "PolymarketWebSocketClient | None":
    """Retourne le client WebSocket global (None si non démarré)."""
    return _ws_client


def set_ws_client(client: "PolymarketWebSocketClient | None") -> None:
    global _ws_client
    _ws_client = client


def get_user_activity(
    user: str,
    limit: int = 100,
    offset: int = 0,
    type_filter: list[str] | None = None,
    side: str | None = None,
    sort_by: str = "TIMESTAMP",
    sort_direction: str = "DESC",
) -> list[dict[str, Any]]:
    """
    Activité on-chain d'un utilisateur (Data API).
    user: adresse du proxy wallet (0x + 40 hex).
    type_filter: TRADE, SPLIT, MERGE, REDEEM, REWARD, CONVERSION, MAKER_REBATE.
    """
    params: dict[str, Any] = {"user": user, "limit": limit, "offset": offset}
    if type_filter:
        params["type"] = type_filter
    if side:
        params["side"] = side
    params["sortBy"] = sort_by
    params["sortDirection"] = sort_direction
    r = requests.get(f"{DATA_API_URL}/activity", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_user_positions(
    user: str,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "TOKENS",
    sort_direction: str = "DESC",
) -> list[dict[str, Any]]:
    """
    Positions ouvertes d'un utilisateur (Data API).
    user: adresse du proxy wallet (0x + 40 hex).
    """
    params = {
        "user": user,
        "limit": limit,
        "offset": offset,
        "sortBy": sort_by,
        "sortDirection": sort_direction,
    }
    r = requests.get(f"{DATA_API_URL}/positions", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


class PolymarketWebSocketClient:
    """
    Client WebSocket pour le Market Channel Polymarket.
    Maintient un cache best_bid / best_ask par token_id (prix temps réel).
    S'exécute dans un thread dédié avec reconnexion automatique.
    """

    def __init__(self, debug_callback: Any = None) -> None:
        self._cache: dict[str, dict[str, float | None]] = {}
        self._lock = threading.Lock()
        self._subscribed: set[str] = set()
        self._ws: Any = None
        self._running = False
        self._thread: threading.Thread | None = None
        # Optionnel : appelé avec le message brut quand on reçoit price_change / last_trade_price / best_bid_ask
        self._debug_callback: Any = debug_callback

    def get_cached(self, token_id: str) -> dict[str, float | None] | None:
        """Retourne {best_bid, best_ask} depuis le cache, ou None si absent."""
        with self._lock:
            out = self._cache.get(token_id)
            return dict(out) if out else None

    def subscribe(self, asset_ids: list[str]) -> None:
        """
        Ajoute des token_ids à l'abonnement ; envoie un message subscribe si déjà connecté.
        Bootstrap : remplit le cache avec les prix REST pour chaque nouvel asset, afin d'avoir
        des prix immédiatement (order book qui bouge vite) avant les mises à jour WebSocket.
        """
        added = [a for a in asset_ids if a and a not in self._subscribed]
        with self._lock:
            self._subscribed.update(added)
        if not added:
            return
        # Bootstrap cache depuis REST pour avoir des prix tout de suite (évite 50/50 ou vide)
        for aid in added:
            try:
                book = get_orderbook(aid)
                if book and (book.get("best_bid") is not None or book.get("best_ask") is not None):
                    if not _looks_stale(book.get("best_bid"), book.get("best_ask")):
                        with self._lock:
                            self._cache[aid] = {
                                "best_bid": book.get("best_bid"),
                                "best_ask": book.get("best_ask"),
                            }
            except Exception as e:
                logger.debug("Bootstrap REST for %s: %s", aid[:20], e)
        try:
            ws = self._ws
            if ws and getattr(ws, "sock", None) and ws.sock and ws.sock.connected:
                ws.send(json.dumps({
                    "assets_ids": added,
                    "operation": "subscribe",
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
        except Exception as e:
            logger.warning("WebSocket subscribe failed: %s", e)

    def _on_message(self, _ws: Any, message: str) -> None:
        try:
            data = json.loads(message) if isinstance(message, str) else message
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        event_type = data.get("event_type") or data.get("eventType")
        with self._lock:
            if event_type == "book":
                self._handle_book(data)
            elif event_type == "price_change":
                self._handle_price_change(data)
            elif event_type == "last_trade_price":
                self._handle_last_trade(data)
            elif event_type == "best_bid_ask":
                self._handle_best_bid_ask(data)
        if self._debug_callback and event_type in ("book", "price_change", "last_trade_price", "best_bid_ask"):
            try:
                self._debug_callback(event_type, data)
            except Exception:
                pass
        return

    def _handle_book(self, data: dict) -> None:
        # Book = snapshot (à la souscription ou quand un trade affecte le carnet).
        # On met à jour le cache si : pas encore de cache, ou le nouveau book a des prix valides (pas stale).
        asset_id = data.get("asset_id") or data.get("assetId")
        if not asset_id:
            return
        bids = data.get("bids") or []
        asks = data.get("asks") or data.get("sells") or []
        best_bid = _best_from_levels(bids, max)
        best_ask = _best_from_levels(asks, min)
        if best_bid is None and best_ask is None:
            return
        bid_n = _normalize_price(best_bid)
        ask_n = _normalize_price(best_ask)
        # Ne pas écraser avec un book stale (50/50 ou 1¢/99¢) si on a déjà de bonnes données
        if asset_id in self._cache and _looks_stale(bid_n, ask_n):
            return
        self._cache[asset_id] = {"best_bid": bid_n, "best_ask": ask_n}

    def _handle_price_change(self, data: dict) -> None:
        for pc in data.get("price_changes") or data.get("priceChanges") or []:
            asset_id = pc.get("asset_id") or pc.get("assetId")
            if not asset_id:
                continue
            bid = pc.get("best_bid") or pc.get("bestBid")
            ask = pc.get("best_ask") or pc.get("bestAsk")
            if bid is not None or ask is not None:
                self._cache[asset_id] = {
                    "best_bid": _normalize_price(float(bid)) if bid is not None else None,
                    "best_ask": _normalize_price(float(ask)) if ask is not None else None,
                }

    def _handle_last_trade(self, data: dict) -> None:
        asset_id = data.get("asset_id") or data.get("assetId")
        price = data.get("price")
        if not asset_id or price is None:
            return
        p = _normalize_price(float(price))
        if p is not None:
            self._cache[asset_id] = {"best_bid": p, "best_ask": p}

    def _handle_best_bid_ask(self, data: dict) -> None:
        asset_id = data.get("asset_id") or data.get("assetId")
        bid = data.get("best_bid") or data.get("bestBid")
        ask = data.get("best_ask") or data.get("bestAsk")
        if not asset_id:
            return
        self._cache[asset_id] = {
            "best_bid": _normalize_price(float(bid)) if bid is not None else None,
            "best_ask": _normalize_price(float(ask)) if ask is not None else None,
        }

    def _on_open(self, ws: Any) -> None:
        self._ws = ws
        ids = list(self._subscribed)
        if ids:
            try:
                ws.send(json.dumps({
                    "assets_ids": ids,
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
            except Exception as e:
                logger.warning("WebSocket initial subscribe failed: %s", e)

    def _on_error(self, _ws: Any, error: Exception) -> None:
        if not self._running:
            return  # arrêt en cours (ex. WinError 10058 sous Windows), ignorer
        logger.warning("WebSocket error: %s", error)

    def _on_close(self, _ws: Any, close_status_code: int | None, close_msg: str | None) -> None:
        self._ws = None

    def _run_loop(self) -> None:
        while self._running:
            try:
                import websocket
                ws = websocket.WebSocketApp(
                    WSS_MARKET_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws = ws
                ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as e:
                logger.warning("WebSocket run_forever failed: %s", e)
            if self._running:
                time.sleep(2)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            ws = self._ws
            if ws:
                if callable(getattr(ws, "close", None)):
                    ws.close()
                elif getattr(ws, "sock", None) and ws.sock:
                    ws.sock.close()
        except Exception:
            pass
        self._ws = None


def _best_from_levels(
    levels: list,
    choose: type,
) -> float | None:
    """Extrait le meilleur prix d'une liste de niveaux (price, size)."""
    prices: list[float] = []
    for row in levels:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            a, b = float(row[0]), float(row[1])
            p = a if (0.01 <= a <= 0.99 or 1 <= a <= 99) else b
            if 0.01 <= p <= 0.99 or 1 <= p <= 99:
                prices.append(p)
        elif isinstance(row, dict):
            p = row.get("price")
            if p is not None:
                prices.append(float(p))
    return choose(prices) if prices else None


def get_event_by_slug(slug: str) -> dict[str, Any] | None:
    """
    Récupère un event par son slug (ex. shifters-vs-natus-vincere-winner).
    GET /events/slug/{slug}
    """
    try:
        r = requests.get(f"{GAMMA_URL}/events/slug/{slug.strip()}", timeout=12)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


# Tag ID Esports LoL (fallback) — GET /tags/slug/league-of-legends → id 65
# Priorité : API /sports (page "games" Polymarket) fournit les tag IDs pour l'esport LoL.
LOL_TAG_ID_FALLBACK = "65"


def _get_sports_metadata() -> list[dict[str, Any]]:
    """Récupère les métadonnées via GET /sports (tags, series par discipline)."""
    try:
        r = requests.get(f"{GAMMA_URL}/sports", timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except requests.RequestException:
        pass
    return []


def _get_lol_tag_ids_from_sports() -> list[str]:
    """
    Récupère les tag IDs LoL depuis l'API /sports (même source que la page games Polymarket).
    Retourne une liste de tag_id (entrée "lol" / "league-of-legends" dans /sports).
    """
    for entry in _get_sports_metadata():
        key = (entry.get("sport") or "").strip().lower()  # clé API "sport"
        if key not in ("lol", "league-of-legends", "league of legends"):
            continue
        tags = entry.get("tags")
        if tags is None:
            continue
        if isinstance(tags, str):
            # "tags" peut être une chaîne comma-separated d'IDs
            return [t.strip() for t in tags.split(",") if t.strip()]
        if isinstance(tags, list):
            return [str(t) for t in tags if t is not None]
    return []


def _get_lol_tag_id_from_tags_slug() -> str | None:
    """Récupère l'ID du tag Esports LoL via GET /tags/slug/league-of-legends (ou lol). Fallback si /sports ne renvoie pas l'esport LoL."""
    for slug in ("league-of-legends", "lol"):
        try:
            r = requests.get(f"{GAMMA_URL}/tags/slug/{slug}", timeout=8)
            if r.status_code == 200:
                data = r.json()
                tid = data.get("id")
                if tid is not None:
                    return str(tid)
        except requests.RequestException:
            continue
    return None


# Mots-clés à exclure : tout ce qui n'est pas LoL esport (football, basket, autres esports CS/SC, etc.)
_NON_LOL_KEYWORDS = (
    "rayo", "atlético", "atletico", "madrid", "vallecano", "la liga", "lal-ray", "lal_",
    "nba", "nfl", "soccer", "football", "basketball", "counter-strike", "cs2", "csgo",
    "starcraft", "star craft", "dota", "valorant", "fifa", "uefa",
    "more markets",  # slug générique Polymarket
)


# Ligues / compétitions LoL esport (toutes régions) — pour _lol_event_filter
_LOL_LEAGUE_KEYWORDS = (
    "lol", "league of legends",
    "lck", "lpl", "lcs", "lec", "lfl", "tcl", "nlc", "cblol", "ljl", "vcs", "pcs", "lco",
    "lcl", "emea masters", "ultraliga", "prime league", "superliga", "hitpoint", "elite",
    "worlds", "msi", "rift rivals",
    "natus vincere", "navi", "g2 esports", "karmine corp", "team vitality", "fnatic",
    "flyquest", "team liquid", "shifters", "mad lions", "rge", "rogue", "bds", "hertics", "heretics",
    "koi", "movistar", "vitality", "sk", "astralis", "xl", "excel",
)

def _lol_event_filter(title: str, slug: str) -> bool:
    """True si l'event est bien un match LoL esport (Sport > Esport > LoL). Exclut football, CS, StarCraft, etc."""
    t = (title or "").lower()
    s = (slug or "").lower()
    combined = t + " " + s
    # Exclure tout ce qui n'est pas LoL (football, autres esports, etc.)
    if any(kw in combined for kw in _NON_LOL_KEYWORDS):
        return False
    if "election" in combined or "elect" in combined:
        return False
    # Doit contenir un indice LoL : ligue, compétition ou " vs " + "winner"
    if any(kw in combined for kw in _LOL_LEAGUE_KEYWORDS):
        return True
    if "winner" in combined and " vs " in combined:
        return True
    return False


def _parse_iso_ts(s: str) -> float:
    """Timestamp (epoch) depuis une chaîne ISO. 0 si absent/invalide."""
    if not s:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _event_start_ts(ev: dict[str, Any]) -> float:
    """Timestamp (epoch) de startDate pour tri. 0 si absent."""
    s = ev.get("startDate") or ev.get("start_date") or ""
    return _parse_iso_ts(s)


def _event_end_ts(ev: dict[str, Any]) -> float:
    """Timestamp (epoch) de endDate. 0 si absent."""
    s = ev.get("endDate") or ev.get("end_date") or ""
    return _parse_iso_ts(s)


def _event_is_live_now(ev: dict[str, Any]) -> bool:
    """True si l'event est en cours (startDate <= now <= endDate). Utilisé pour choisir le bon marché parmi plusieurs matchs mêmes équipes."""
    import time
    now = time.time()
    start = _event_start_ts(ev)
    end = _event_end_ts(ev)
    if start == 0 or end == 0:
        return False
    return start <= now <= end


def _normalize_team(s: str) -> str:
    """Normalise un nom d'équipe pour le matching (lowercase, strip)."""
    return (s or "").lower().strip()


def _team_name_in_title(team_name: str, title_lower: str) -> bool:
    """True si le nom d'équipe (ou un mot significatif) apparaît dans le titre de l'event."""
    if not team_name or not title_lower:
        return False
    n = _normalize_team(team_name)
    if n in title_lower:
        return True
    # Abréviations (Polymarket utilise souvent le court : NaVi, VKS, PNG)
    abrev = {
        "natus vincere": "navi",
        "team vitality": "vitality",
        "g2 esports": "g2",
        "karmine corp": "karmine",
        "karmine corp blue": "karmine",
        "vivo keyd stars": "vks",
        "pain gaming": "pain",
    }
    for long_form, short in abrev.items():
        if long_form in n and short in title_lower:
            return True
    if "keyd" in n and "keyd" in title_lower:
        return True
    if "pain" in n and ("pain" in title_lower or "png" in title_lower):
        return True
    # Un mot significatif du nom (ex. "Vitality" dans "Team Vitality")
    for word in n.split():
        if len(word) > 2 and word in title_lower:
            return True
    return False


def _event_matches_teams(ev: dict[str, Any], team_blue: str, team_red: str) -> bool:
    """True si l'event Polymarket correspond au match (mêmes équipes bleue/rouge)."""
    title = (ev.get("title") or "") + " " + (ev.get("slug") or "")
    title_lower = title.lower()
    return _team_name_in_title(team_blue, title_lower) and _team_name_in_title(team_red, title_lower)


def _is_match_slug(slug: str) -> bool:
    """
    True si le slug suit le format match Polymarket : lol-{abbrev1}-{abbrev2}-{YYYY-MM-DD}.
    Ces slugs pointent vers le bon order book (match winner), pas vers un « season winner ».
    """
    if not slug or not isinstance(slug, str):
        return False
    import re
    return bool(re.match(r"^lol-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$", slug.strip().lower()))


def suggest_slug_for_teams(
    team_blue: str,
    team_red: str,
    *,
    limit: int = 200,
    upcoming_only: bool = True,
) -> tuple[str | None, str | None]:
    """
    Suggère le slug Polymarket correspondant au match (équipes bleue/rouge).
    Priorité : event en cours (live), sinon le prochain à venir.
    Retourne (slug, title) ou (None, None) si aucun match.
    """
    team_blue = (team_blue or "").strip()
    team_red = (team_red or "").strip()
    if not team_blue or not team_red:
        return (None, None)
    n_blue = _normalize_team(team_blue)
    n_red = _normalize_team(team_red)
    if n_blue in ("équipe bleue", "team blue", "bleue") or n_red in ("équipe rouge", "team red", "rouge"):
        return (None, None)
    events = get_events_lol(limit=limit, upcoming_only=upcoming_only, main_leagues_only=False)
    matching = [ev for ev in events if _event_matches_teams(ev, team_blue, team_red) and _is_match_event(ev)]
    if not matching:
        return (None, None)
    # Préférer : 1) event en cours (live), 2) slug au format lol-xxx-yyy-YYYY-MM-DD (bon order book), 3) startDate
    def sort_key(ev: dict[str, Any]) -> tuple[int, int, float]:
        live = 0 if _event_is_live_now(ev) else 1
        slug_ok = 0 if _is_match_slug(ev.get("slug") or "") else 1
        start = _event_start_ts(ev)
        return (live, slug_ok, start)
    matching.sort(key=sort_key)
    best = matching[0]
    return (best.get("slug") or None, best.get("title") or None)


def _is_match_event(ev: dict[str, Any]) -> bool:
    """True si l'event est un match (Team A vs Team B, BO1/BO3/BO5), pas un 'Season Winner' / 'Tournament Winner'."""
    title = (ev.get("title") or "").lower()
    if " vs " not in title:
        return False
    if "season winner" in title or "tournament winner" in title or "region" in title or "playoffs winner" in title:
        return False
    if "(bo1)" in title or "(bo3)" in title or "(bo5)" in title:
        return True
    if "winner" in title and "vs" in title:
        return True
    return True


# Ligues principales affichées sur lolesports.com/live (LCK, LPL, LEC, LCS, Worlds, MSI)
_MAIN_LEAGUE_KEYWORDS = ("lck", "lpl", "lec", "lcs", "worlds", "msi")

# Cup / Play-In régionaux (on les exclut par défaut pour afficher les bons matchs — saison régulière, pas LCK Cup Play-In)
_REGIONAL_CUP_PLAYIN = ("cup play-in", "lck cup", "lpl cup", "lec cup", "lcs cup", " cup ", "-cup-", "_cup_")


def _is_regional_cup_or_playin(ev: dict[str, Any]) -> bool:
    """True si l'event est un Cup / Play-In régional (LCK Cup Play-In, etc.), pas saison régulière ni Worlds/MSI."""
    t = (ev.get("title") or "").lower()
    s = (ev.get("slug") or "").lower()
    combined = t + " " + s
    if "worlds" in combined or "msi" in combined:
        return False  # on garde Worlds et MSI
    return any(kw in combined for kw in _REGIONAL_CUP_PLAYIN)


def _is_main_league_event(ev: dict[str, Any]) -> bool:
    """True si l'event concerne une ligue principale (LCK, LPL, LEC, LCS, Worlds, MSI)."""
    t = (ev.get("title") or "").lower()
    s = (ev.get("slug") or "").lower()
    combined = t + " " + s
    return any(kw in combined for kw in _MAIN_LEAGUE_KEYWORDS)


def get_events_lol(
    limit: int = 50,
    upcoming_only: bool = True,
    main_leagues_only: bool = False,
) -> list[dict[str, Any]]:
    """
    Liste des events (marchés) LoL / League of Legends — Esport uniquement (Sport > Esport > LoL).
    Priorité : tag Esport LoL (GET /tags/slug/league-of-legends), puis /sports en secours.
    Si upcoming_only=True, ne garde que les événements dont la fin est aujourd'hui ou plus tard.
    Si main_leagues_only=True, ne garde que LCK, LPL, LEC, LCS, Worlds, MSI (ligues sur lolesports.com/live).
    Tri : matchs (X vs Y, BO1/BO3/BO5) en premier, puis par startDate croissant.
    """
    from datetime import datetime, timezone

    params_base = {
        "limit": max(limit, 100),
        "closed": "false",
        "active": "true",
        "order": "startDate",
        "ascending": "true",
    }
    params_with_date = dict(params_base)
    if upcoming_only:
        now = datetime.now(timezone.utc)
        today_iso = now.strftime("%Y-%m-%dT00:00:00Z")
        params_with_date["start_date_min"] = today_iso

    events: list[dict[str, Any]] = []

    # 1) Priorité : tag Esport LoL (Sport > Esport > LoL) — GET /tags/slug/league-of-legends
    tag_id = _get_lol_tag_id_from_tags_slug() or LOL_TAG_ID_FALLBACK
    if tag_id:
        try:
            r = requests.get(
                f"{GAMMA_URL}/events",
                params={**params_with_date, "tag_id": tag_id},
                timeout=12,
            )
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and raw:
                    events = raw
        except requests.RequestException:
            pass

    # 2) tag_slug (même section Esport > LoL)
    if not events:
        for tag_slug in ("league-of-legends", "lol"):
            try:
                r = requests.get(
                    f"{GAMMA_URL}/events",
                    params={**params_with_date, "tag_slug": tag_slug},
                    timeout=12,
                )
                if r.status_code == 200:
                    raw = r.json()
                    if isinstance(raw, list) and raw:
                        events = raw
                        break
            except requests.RequestException:
                continue

    # 3) Sans start_date_min (au cas où l'API renvoie vide avec la date)
    if not events and tag_id:
        try:
            r = requests.get(
                f"{GAMMA_URL}/events",
                params={**params_base, "tag_id": tag_id},
                timeout=12,
            )
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and raw:
                    events = raw
        except requests.RequestException:
            pass

    # 4) Fallback : tag IDs depuis /sports (page "games" Polymarket)
    if not events:
        lol_tag_ids = _get_lol_tag_ids_from_sports()
        for tid in lol_tag_ids:
            if not tid:
                continue
            try:
                r = requests.get(
                    f"{GAMMA_URL}/events",
                    params={**params_with_date, "tag_id": tid},
                    timeout=12,
                )
                if r.status_code == 200:
                    raw = r.json()
                    if isinstance(raw, list) and raw:
                        events = raw
                        break
            except requests.RequestException:
                continue

    if not events:
        try:
            r = requests.get(
                f"{GAMMA_URL}/events",
                params={**params_with_date, "limit": 500},
                timeout=15,
            )
            if r.status_code == 200:
                raw = r.json() or []
                for ev in raw:
                    t, s = ev.get("title") or "", ev.get("slug") or ""
                    if _lol_event_filter(t, s):
                        events.append(ev)
        except requests.RequestException:
            pass

    # Filtrer : ne garder que LoL esport (Sport > Esport > LoL). Exclut football, CS, StarCraft, etc.
    if events:
        events = [
            ev for ev in events
            if _lol_event_filter(ev.get("title") or "", ev.get("slug") or "")
        ]

    # Filtrer : ne garder que les events non terminés (endDate >= maintenant)
    if upcoming_only and events:
        now_ts = datetime.now(timezone.utc).timestamp()
        filtered = []
        for ev in events:
            end_ts = _event_end_ts(ev)
            if end_ts == 0 or end_ts >= now_ts:
                filtered.append(ev)
        events = filtered

    # Filtrer : ligues principales (LCK, LPL, LEC, LCS, Worlds, MSI), sans Cup/Play-In régionaux (LCK Cup Play-In, etc.)
    if main_leagues_only and events:
        events = [
            ev for ev in events
            if _is_main_league_event(ev) and not _is_regional_cup_or_playin(ev)
        ]

    # Trier : 1) matchs (vs, BO) en premier, 2) par startDate croissant (prochains matchs en tête)
    def sort_key(ev: dict[str, Any]) -> tuple[int, float]:
        is_match = 0 if _is_match_event(ev) else 1
        ts = _event_start_ts(ev)
        return (is_match, ts)

    events.sort(key=sort_key)
    return events[:limit]


def _outcome_names(market: dict[str, Any]) -> list[str]:
    """Extrait les noms des outcomes d'un market (sans les token_ids)."""
    outcomes = market.get("outcomes") or market.get("shortOutcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = outcomes.split(",") if "," in outcomes else [outcomes.strip(), ""]
    if isinstance(outcomes, list) and len(outcomes) >= 2:
        return [str(x).strip() for x in outcomes[:2]]
    return []


def _is_over_under_market(market: dict[str, Any]) -> bool:
    """True si le market est Over/Under (pas Winner équipe A vs équipe B)."""
    names = _outcome_names(market)
    if len(names) < 2:
        return False
    low = [n.lower() for n in names]
    return ("over" in low[0] and "under" in low[1]) or ("over" in low[1] and "under" in low[0])


def _is_prop_market(market: dict[str, Any]) -> bool:
    """True si le market est un pari annexe (First Blood, Total Kills, etc.), pas le Winner du match. Toutes ligues (LEC, CBLOL, LCK, etc.)."""
    q = (market.get("question") or market.get("title") or "").lower()
    # First Blood, Total Kills Over/Under, etc. — même si les outcomes sont des noms d'équipes
    if "first blood" in q:
        return True
    if "total kills" in q or "number of kills" in q:
        return True
    if "over/under" in q or "over under" in q:
        return True
    if "first tower" in q or "first dragon" in q or "first baron" in q:
        return True
    if "map " in q and "winner" not in q:  # "Map 1 winner" parfois = match, "Map 1 duration" = prop
        if "duration" in q or "time" in q or "length" in q:
            return True
    if "duration" in q or "game length" in q or "match length" in q:
        return True
    if "handicap" in q or "spread" in q or "correct score" in q:
        return True
    if "baron" in q or "dragon" in q or "tower" in q:
        if "first " in q or "total " in q or "number of" in q:
            return True
    return False


def _is_main_match_winner_market(market: dict[str, Any]) -> bool:
    """True si le market est le Winner principal du match (ex. 'LoL: Team A vs Team B (BO1) - LEC')."""
    q = (market.get("question") or market.get("title") or "").lower()
    if " vs " not in q:
        return False
    # Le marché principal a typiquement (BO1), (BO3), (BO5) dans la question
    if "(bo1)" in q or "(bo3)" in q or "(bo5)" in q:
        return True
    # Fallback : contient "winner" ou pas de "first blood" / "total kills" (déjà exclus par _is_prop_market)
    return not _is_prop_market(market)


def _select_winner_market(markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Choisit le market « Winner » du match (équipe A vs équipe B), pas First Blood ni Over/Under.
    Priorité : marché dont la question contient " vs " et "(BO1)"/(BO3)/(BO5) (ex. LoL: Team A vs Team B (BO1) - LEC).
    """
    if not markets:
        return None
    # 1) Préférer le marché principal du match (vs + BO1/BO3/BO5)
    for m in markets:
        if not isinstance(m, dict):
            continue
        if _is_main_match_winner_market(m):
            return m
    # 2) Sinon premier marché qui n'est ni Over/Under ni prop (First Blood, Total Kills, etc.)
    for m in markets:
        if not isinstance(m, dict):
            continue
        if _is_over_under_market(m) or _is_prop_market(m):
            continue
        return m
    return markets[0] if isinstance(markets[0], dict) else None


def parse_market_tokens(market: dict[str, Any]) -> tuple[list[str], list[str]]:
    """
    Extrait les token_ids CLOB et les noms d'outcomes pour un market binaire (A vs B).
    Retourne (token_ids, outcome_names).
    """
    token_ids: list[str] = []
    outcome_names: list[str] = []
    clob = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(clob, str):
        try:
            clob = json.loads(clob)
        except json.JSONDecodeError:
            clob = []
    if isinstance(clob, list) and len(clob) >= 2:
        token_ids = [str(x) for x in clob[:2]]
    outcomes = market.get("outcomes") or market.get("shortOutcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = outcomes.split(",") if "," in outcomes else [outcomes.strip(), ""]
    if isinstance(outcomes, list) and len(outcomes) >= 2:
        outcome_names = [str(x).strip() for x in outcomes[:2]]
    return (token_ids, outcome_names)


def _normalize_price(p: float | None) -> float | None:
    """Prix 0–1 ou en centimes (1–100) → toujours 0–1."""
    if p is None:
        return None
    return p / 100.0 if p > 1 else p


def get_market_price(token_id: str) -> dict[str, float | None]:
    """
    Récupère le prix marché (best bid, best ask) via l'API Pricing CLOB.
    GET /price?token_id=...&side=BUY  → best ask ; side=SELL → best bid.
    Peut renvoyer None pour certains marchés (ex. Over/Under) ou si l'endpoint n'existe pas.
    """
    import time
    best_bid: float | None = None
    best_ask: float | None = None
    params_base = {"token_id": token_id, "_": int(time.time() * 1000)}
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    try:
        for side, key in (("SELL", "best_bid"), ("BUY", "best_ask")):
            r = requests.get(
                f"{CLOB_URL}/price",
                params={**params_base, "side": side},
                timeout=8,
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json()
                raw = data.get("price")
                if raw is not None:
                    val = float(raw)
                    if key == "best_bid":
                        best_bid = _normalize_price(val)
                    else:
                        best_ask = _normalize_price(val)
        return {"best_bid": best_bid, "best_ask": best_ask}
    except (requests.RequestException, (ValueError, TypeError)):
        return {"best_bid": None, "best_ask": None}


def _get_orderbook_from_book_api(token_id: str) -> dict[str, float | None]:
    """
    Fallback : récupère best_bid / best_ask depuis GET /book.
    Best bid = max des prix d'achat, best ask = min des prix de vente.
    """
    import time
    try:
        r = requests.get(
            f"{CLOB_URL}/book",
            params={"token_id": token_id, "_": int(time.time() * 1000)},
            timeout=8,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        if r.status_code != 200:
            return {"best_bid": None, "best_ask": None}
        data = r.json()
        bids = data.get("bids") or data.get("buys") or []
        asks = data.get("asks") or data.get("sells") or []

        def extract_prices(rows: list) -> list[float]:
            out: list[float] = []
            for row in rows:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    # Polymarket peut renvoyer [price, size] ou [size, price] ; le prix est en 0–1 ou 1–99
                    a, b = float(row[0]), float(row[1])
                    p = a if (0.01 <= a <= 0.99 or 1 <= a <= 99) else b
                    if 0.01 <= p <= 0.99 or 1 <= p <= 99:
                        out.append(p)
                elif isinstance(row, dict):
                    p = row.get("price")
                    if p is not None:
                        out.append(float(p))
            return out

        bid_prices = extract_prices(bids)
        ask_prices = extract_prices(asks)
        best_bid = _normalize_price(max(bid_prices)) if bid_prices else None
        best_ask = _normalize_price(min(ask_prices)) if ask_prices else None
        return {"best_bid": best_bid, "best_ask": best_ask}
    except (requests.RequestException, (ValueError, TypeError, KeyError)):
        return {"best_bid": None, "best_ask": None}


def get_orderbook_depth(token_id: str) -> dict[str, list[tuple[float, float]]]:
    """
    Récupère la profondeur du carnet (GET /book) : liste de (prix, size) par niveau.
    asks = vendeurs (tri asc par prix), bids = acheteurs (tri desc).
    Prix en 0–1, size en nombre de parts (contrats).
    """
    import time
    out: dict[str, list[tuple[float, float]]] = {"asks": [], "bids": []}
    try:
        r = requests.get(
            f"{CLOB_URL}/book",
            params={"token_id": token_id, "_": int(time.time() * 1000)},
            timeout=8,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        if r.status_code != 200:
            return out
        data = r.json()
        bids_raw = data.get("bids") or data.get("buys") or []
        asks_raw = data.get("asks") or data.get("sells") or []

        def parse_levels(rows: list) -> list[tuple[float, float]]:
            levels: list[tuple[float, float]] = []
            for row in rows:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    a, b = float(row[0]), float(row[1])
                    if 0.001 <= a <= 1.5 or 0.1 <= a <= 99:
                        p = _normalize_price(a) if a > 1 else a
                        size = b
                    else:
                        p = _normalize_price(b) if b > 1 else b
                        size = a
                    if 0 < p <= 1 and size > 0:
                        levels.append((p, size))
                elif isinstance(row, dict):
                    p = row.get("price")
                    s = row.get("size")
                    if p is not None and s is not None:
                        pv = float(p)
                        if pv > 1:
                            pv = pv / 100.0
                        if 0 < pv <= 1 and float(s) > 0:
                            levels.append((pv, float(s)))
            return levels

        asks = parse_levels(asks_raw)
        bids = parse_levels(bids_raw)
        asks.sort(key=lambda x: x[0])
        bids.sort(key=lambda x: x[0], reverse=True)
        out["asks"] = asks
        out["bids"] = bids
    except (requests.RequestException, (ValueError, TypeError, KeyError)):
        pass
    return out


def _get_last_trade_price(token_id: str) -> float | None:
    """
    GET /last-trade-price?token_id=... — dernier prix d'exécution (souvent plus à jour que midpoint).
    Le site Polymarket affiche typiquement ce type de prix (ex. Vitality 19¢, G2 85¢).
    """
    import time
    try:
        r = requests.get(
            f"{CLOB_URL}/last-trade-price",
            params={"token_id": token_id, "_": int(time.time() * 1000)},
            timeout=8,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        if r.status_code != 200:
            return None
        data = r.json()
        raw = data.get("price") or data.get("lastTradePrice")
        if raw is None:
            return None
        p = float(raw)
        return _normalize_price(p)
    except (requests.RequestException, (ValueError, TypeError)):
        return None


def _get_midpoint(token_id: str) -> float | None:
    """Fallback : GET /midpoint?token_id=... pour un prix médian (utilisé pour bid et ask si rien d'autre)."""
    import time
    try:
        r = requests.get(
            f"{CLOB_URL}/midpoint",
            params={"token_id": token_id, "_": int(time.time() * 1000)},
            timeout=8,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        if r.status_code != 200:
            return None
        data = r.json()
        raw = data.get("mid") or data.get("midpoint") or data.get("price")
        if raw is None:
            return None
        p = float(raw)
        return _normalize_price(p)
    except (requests.RequestException, (ValueError, TypeError)):
        return None


def _is_inverted_book(bid: float | None, ask: float | None) -> bool:
    """True si le book est « inversé » (bid très bas, ask très haut) comme 3¢/99¢ — convention API possible."""
    if bid is None or ask is None:
        return False
    return bid < 0.20 and ask > 0.80


def _inverted_to_display(bid: float | None, ask: float | None) -> tuple[float | None, float | None]:
    """
    Quand le flux envoie 3¢/99¢ (book inversé), le site peut afficher le complément.
    On utilise 1 - ask comme prix « acheter » et 1 - bid comme « vendre » pour ce outcome.
    """
    if bid is None or ask is None:
        return (None, None)
    display_bid = 1.0 - ask
    display_ask = 1.0 - bid
    if 0 <= display_bid <= 1 and 0 <= display_ask <= 1:
        return (_normalize_price(display_bid), _normalize_price(display_ask))
    return (None, None)


def _looks_stale(bid: float | None, ask: float | None) -> bool:
    """
    True si les prix sont obsolètes ou incohérents (on préfère alors /midpoint).
    - 1¢/99¢ : données CLOB obsolètes
    - bid > ask : impossible dans un carnet normal (API renvoie parfois l'inverse)
    - ask très bas et bid très haut : même symptôme (ex. ask 4¢, bid 98¢)
    """
    if bid is None and ask is None:
        return False
    if bid is not None and ask is not None:
        if bid > ask:
            return True  # spread inversé = données incohérentes
        if bid <= 0.02 and ask >= 0.98:
            return True  # 1¢/99¢
        if ask <= 0.05 and bid >= 0.95:
            return True  # ex. ask 4¢ bid 98¢ = incohérent
    if bid is not None and bid <= 0.02:
        return True
    if ask is not None and ask >= 0.98:
        return True
    return False


def _single_price_to_book(price: float | None) -> dict[str, Any] | None:
    """Utilise un seul prix (ex. last-trade) pour bid et ask."""
    if price is None or not (0 <= price <= 1):
        return None
    return {"best_bid": price, "best_ask": price}


def get_orderbook(token_id: str) -> dict[str, Any] | None:
    """
    Récupère best_bid / best_ask : 1) /price, 2) /book, 3) /last-trade-price (souvent le plus à jour), 4) /midpoint.
    Si données stale (1¢/99¢ ou bid>ask), on utilise last-trade-price puis midpoint.
    """
    out = get_market_price(token_id)
    if out.get("best_bid") is not None or out.get("best_ask") is not None:
        if _looks_stale(out.get("best_bid"), out.get("best_ask")):
            last = _get_last_trade_price(token_id)
            if last is not None:
                return _single_price_to_book(last)
            mid = _get_midpoint(token_id)
            if mid is not None:
                return _single_price_to_book(mid)
        return out
    out = _get_orderbook_from_book_api(token_id)
    if out.get("best_bid") is not None or out.get("best_ask") is not None:
        if _looks_stale(out.get("best_bid"), out.get("best_ask")):
            last = _get_last_trade_price(token_id)
            if last is not None:
                return _single_price_to_book(last)
            mid = _get_midpoint(token_id)
            if mid is not None:
                return _single_price_to_book(mid)
        return out
    last = _get_last_trade_price(token_id)
    if last is not None:
        return _single_price_to_book(last)
    mid = _get_midpoint(token_id)
    if mid is not None:
        return _single_price_to_book(mid)
    return out


def _parse_outcome_prices(market: dict[str, Any]) -> list[float] | None:
    """
    Extrait les prix par outcome depuis l'API Gamma (outcomePrices).
    Accepte : liste ["0.35", "0.65"], chaîne "0.35,0.65", ou chaîne JSON '["0.35","0.65"]'.
    Retourne [p0, p1] en 0–1 ou None si absent/invalide.
    """
    raw = market.get("outcomePrices") or market.get("outcome_prices")
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
    if isinstance(raw, str):
        parts = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list) and len(raw) >= 2:
        parts = [str(raw[0]), str(raw[1])]
    else:
        return None
    if len(parts) < 2:
        return None
    out: list[float] = []
    for s in parts[:2]:
        try:
            p = float(s)
            if p > 1:
                p = p / 100.0
            if 0 <= p <= 1:
                out.append(p)
            else:
                return None
        except (ValueError, TypeError):
            return None
    return out if len(out) == 2 else None


def get_prices_for_event(event: dict[str, Any]) -> list[tuple[str, dict[str, float | None]]]:
    """
    Pour un event Polymarket, récupère les prix pour chaque outcome.
    Priorité 1 : cache WebSocket (Market Channel, temps réel) si disponible.
    Priorité 2 : outcomePrices Gamma puis CLOB REST (/price, /book, /last-trade-price, /midpoint).
    """
    markets = event.get("markets") or []
    if not markets:
        return []
    market = _select_winner_market(markets)
    if not market:
        return []
    outcome_names = _outcome_names(market)
    name0 = outcome_names[0] if outcome_names else "Outcome 1"
    name1 = outcome_names[1] if len(outcome_names) > 1 else "Outcome 2"

    token_ids, outcome_names = parse_market_tokens(market)
    if len(token_ids) < 2:
        empty = {"best_bid": None, "best_ask": None}
        return [(name0, empty), (name1, empty)]

    # Priorité : cache WebSocket (prix temps réel) uniquement si les deux outcomes sont cohérents
    # Sinon le WS renvoie souvent un book initial 98¢/50¢ (stale) ; on préfère REST dans ce cas.
    client = get_ws_client()
    if client is not None:
        client.subscribe(token_ids)
        cached0 = client.get_cached(token_ids[0])
        cached1 = client.get_cached(token_ids[1])
        if cached0 is not None and cached1 is not None:
            dict0 = {"best_bid": cached0.get("best_bid"), "best_ask": cached0.get("best_ask")}
            dict1 = {"best_bid": cached1.get("best_bid"), "best_ask": cached1.get("best_ask")}
            stale0 = _looks_stale(dict0.get("best_bid"), dict0.get("best_ask"))
            stale1 = _looks_stale(dict1.get("best_bid"), dict1.get("best_ask"))
            if not stale0 and not stale1:
                return [(name0, dict0), (name1, dict1)]
            if stale0 and stale1:
                # Book « inversé » (ex. 3¢/99¢, 1¢/97¢) : essayer le complément 1-p comme sur l’UI
                inv0 = _is_inverted_book(dict0.get("best_bid"), dict0.get("best_ask"))
                inv1 = _is_inverted_book(dict1.get("best_bid"), dict1.get("best_ask"))
                result0, result1 = dict0, dict1
                if inv0:
                    d0 = _inverted_to_display(dict0.get("best_bid"), dict0.get("best_ask"))
                    if d0[0] is not None:
                        result0 = {"best_bid": d0[0], "best_ask": d0[1]}
                if inv1:
                    d1 = _inverted_to_display(dict1.get("best_bid"), dict1.get("best_ask"))
                    if d1[0] is not None:
                        result1 = {"best_bid": d1[0], "best_ask": d1[1]}
                if not _looks_stale(result0.get("best_bid"), result0.get("best_ask")) and not _looks_stale(result1.get("best_bid"), result1.get("best_ask")):
                    return [(name0, result0), (name1, result1)]
                mid0 = _get_midpoint(token_ids[0])
                mid1 = _get_midpoint(token_ids[1])
                if mid0 is not None and mid1 is not None and 0.02 <= mid0 <= 0.98 and 0.02 <= mid1 <= 0.98:
                    dict0 = {"best_bid": mid0, "best_ask": mid0}
                    dict1 = {"best_bid": mid1, "best_ask": mid1}
                    return [(name0, dict0), (name1, dict1)]
                if mid0 is not None and 0.05 <= mid0 <= 0.95:
                    return [
                        (name0, {"best_bid": mid0, "best_ask": mid0}),
                        (name1, {"best_bid": 1.0 - mid0, "best_ask": 1.0 - mid0}),
                    ]
            # Un seul stale (ex. 98¢/50¢) → ne pas faire confiance au cache, passer au REST

    # Priorité : CLOB REST (order book réel) — évite 100% / 0.1% venant de Gamma outcomePrices
    book0 = get_orderbook(token_ids[0])
    book1 = get_orderbook(token_ids[1])
    dict0 = {"best_bid": book0.get("best_bid") if book0 else None, "best_ask": book0.get("best_ask") if book0 else None}
    dict1 = {"best_bid": book1.get("best_bid") if book1 else None, "best_ask": book1.get("best_ask") if book1 else None}
    stale0 = _looks_stale(dict0.get("best_bid"), dict0.get("best_ask"))
    stale1 = _looks_stale(dict1.get("best_bid"), dict1.get("best_ask"))
    if not stale0 and not stale1:
        return [(name0, dict0), (name1, dict1)]
    if stale0 and stale1:
        # Correction book inversé par outcome (même logique que le cache WebSocket)
        inv0 = _is_inverted_book(dict0.get("best_bid"), dict0.get("best_ask"))
        inv1 = _is_inverted_book(dict1.get("best_bid"), dict1.get("best_ask"))
        result0, result1 = dict0, dict1
        if inv0:
            d0 = _inverted_to_display(dict0.get("best_bid"), dict0.get("best_ask"))
            if d0[0] is not None:
                result0 = {"best_bid": d0[0], "best_ask": d0[1]}
        if inv1:
            d1 = _inverted_to_display(dict1.get("best_bid"), dict1.get("best_ask"))
            if d1[0] is not None:
                result1 = {"best_bid": d1[0], "best_ask": d1[1]}
        if not _looks_stale(result0.get("best_bid"), result0.get("best_ask")) and not _looks_stale(result1.get("best_bid"), result1.get("best_ask")):
            return [(name0, result0), (name1, result1)]
        mid0 = _get_midpoint(token_ids[0])
        mid1 = _get_midpoint(token_ids[1])
        if mid0 is not None and mid1 is not None and 0.02 <= mid0 <= 0.98 and 0.02 <= mid1 <= 0.98:
            dict0 = {"best_bid": mid0, "best_ask": mid0}
            dict1 = {"best_bid": mid1, "best_ask": mid1}
            return [(name0, dict0), (name1, dict1)]
        if mid0 is not None and 0.05 <= mid0 <= 0.95:
            dict0 = {"best_bid": mid0, "best_ask": mid0}
            dict1 = {"best_bid": 1.0 - mid0, "best_ask": 1.0 - mid0}
            return [(name0, dict0), (name1, dict1)]
    # Si un seul outcome a des prix valides, on les garde (l'autre reste null ou stale)

    # Dernier recours : Gamma outcomePrices — uniquement si pas extrêmes (sinon on affiche 100% / 0.1%)
    gamma_prices = _parse_outcome_prices(market)
    if gamma_prices is not None and len(gamma_prices) >= 2:
        p0, p1 = gamma_prices[0], gamma_prices[1]
        if 0.02 <= p0 <= 0.98 and 0.02 <= p1 <= 0.98 and abs(p0 + p1 - 1.0) < 0.15:
            return [
                (name0, {"best_bid": p0, "best_ask": p0}),
                (name1, {"best_bid": p1, "best_ask": p1}),
            ]

    return [(name0, dict0), (name1, dict1)]
