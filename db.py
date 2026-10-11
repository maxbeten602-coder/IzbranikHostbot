"""Folder-based user database.

Structure:
  db/users/{telegram_id}.json
  db/drops.json
  db/withdraws.json
  db/stats/global.json
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(os.getenv("NX_DATA_DIR") or (Path(__file__).resolve().parent.parent / "db"))
USERS = ROOT / "users"
DROPS = ROOT / "drops.json"
WITHDRAWS = ROOT / "withdraws.json"
GLOBAL = ROOT / "stats" / "global.json"
PROMOS = ROOT / "promos.json"

USERS.mkdir(parents=True, exist_ok=True)
(ROOT / "stats").mkdir(parents=True, exist_ok=True)


def _read(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def user_path(uid: int | str) -> Path:
    return USERS / f"{uid}.json"


def default_user(uid: int, first_name: str = "", username: str = "", photo_url: str | None = None) -> dict:
    return {
        "id": int(uid),
        "username": username or "",
        "first_name": first_name or "",
        "photo_url": photo_url,
        "balance": 0,
        "inventory": [],
        "last_free": 0,
        "total_deposited": 0,
        "total_spent": 0,
        "stats": {
            "cases_opened": 0,
            "free_opened": 0,
            "games_played": 0,
            "pickaxe_plays": 0,
            "wins": 0,
            "losses": 0,
            "items_sold": 0,
            "best_drop": None,
            "best_drop_value": 0,
        },
        "created_at": int(time.time() * 1000),
        "updated_at": int(time.time() * 1000),
        "last_active": int(time.time() * 1000),
    }


def get_user(uid: int | str) -> Optional[dict]:
    p = user_path(uid)
    if not p.exists():
        return None
    data = _read(p, None)
    return data if isinstance(data, dict) else None


def ensure_user(uid: int, first_name: str = "", username: str = "", photo_url: str | None = None) -> dict:
    existing = get_user(uid)
    if existing:
        changed = False
        if first_name and existing.get("first_name") != first_name:
            existing["first_name"] = first_name
            changed = True
        if username and existing.get("username") != username:
            existing["username"] = username
            changed = True
        if photo_url and existing.get("photo_url") != photo_url:
            existing["photo_url"] = photo_url
            changed = True
        if "stats" not in existing:
            existing["stats"] = default_user(uid)["stats"]
            changed = True
        if changed:
            existing["updated_at"] = int(time.time() * 1000)
            save_user(existing)
        return existing
    u = default_user(uid, first_name, username, photo_url)
    save_user(u)
    return u


def save_user(data: dict) -> dict:
    data["updated_at"] = int(time.time() * 1000)
    data["last_active"] = int(time.time() * 1000)
    _write(user_path(data["id"]), data)
    return data


def _num(x: float):
    """4 знака после запятой; целые числа храним как int."""
    x = round(float(x), 4)
    return int(x) if x == int(x) else x


def add_balance(uid: int, amount: float, deposited: bool = False) -> dict:
    u = ensure_user(uid)
    u["balance"] = _num(float(u.get("balance", 0) or 0) + float(amount))
    if deposited and amount > 0:
        u["total_deposited"] = _num(float(u.get("total_deposited", 0) or 0) + float(amount))
    return save_user(u)


def set_balance(uid: int, balance: int) -> dict:
    u = ensure_user(uid)
    u["balance"] = int(balance)
    return save_user(u)


def spend(uid: int, amount: int) -> tuple[bool, dict]:
    u = ensure_user(uid)
    if int(u.get("balance", 0)) < amount:
        return False, u
    u["balance"] = int(u["balance"]) - amount
    u["total_spent"] = int(u.get("total_spent", 0)) + amount
    return True, save_user(u)


def add_item(uid: int, item: dict) -> dict:
    u = ensure_user(uid)
    inv = u.get("inventory") or []
    inv.insert(0, item)
    u["inventory"] = inv
    stats = u.setdefault("stats", {})
    if item.get("value", 0) > stats.get("best_drop_value", 0):
        stats["best_drop"] = item.get("name")
        stats["best_drop_value"] = item.get("value", 0)
    return save_user(u)


def remove_item(uid: int, item_id) -> tuple[Optional[dict], dict]:
    u = ensure_user(uid)
    inv = u.get("inventory") or []
    found = None
    new_inv = []
    for it in inv:
        if found is None and str(it.get("id")) == str(item_id):
            found = it
            continue
        new_inv.append(it)
    u["inventory"] = new_inv
    save_user(u)
    return found, u


def bump_stat(uid: int, key: str, n: int = 1) -> dict:
    u = ensure_user(uid)
    stats = u.setdefault("stats", {})
    stats[key] = int(stats.get(key, 0)) + n
    return save_user(u)


def find_by_username(username: str) -> Optional[dict]:
    username = (username or "").lstrip("@").lower()
    if not username:
        return None
    for p in USERS.glob("*.json"):
        d = _read(p, {})
        if (d.get("username") or "").lower() == username:
            return d
    return None


def list_users(limit: int = 100) -> list:
    out = []
    for p in sorted(USERS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        d = _read(p, None)
        if isinstance(d, dict):
            out.append(d)
        if len(out) >= limit:
            break
    return out


def top_spent(limit: int = 20) -> list:
    users = list_users(500)
    users.sort(key=lambda u: int(u.get("total_spent", 0)), reverse=True)
    return [u for u in users if int(u.get("total_spent", 0)) > 0][:limit]


def add_drop(uid: int, name: str, prize: str, value: int):
    drops = _read(DROPS, [])
    drops.insert(0, {
        "uid": uid, "name": name, "prize": prize, "value": value,
        "ts": int(time.time() * 1000)
    })
    _write(DROPS, drops[:100])


def recent_drops(limit: int = 20) -> list:
    return _read(DROPS, [])[:limit]


def add_withdraw(data: dict) -> dict:
    items = _read(WITHDRAWS, [])
    data["id"] = (max([int(w.get("id") or 0) for w in items]) + 1) if items else 1
    data["ts"] = int(time.time() * 1000)
    data["status"] = data.get("status", "pending")
    items.append(data)
    _write(WITHDRAWS, items)
    return data


def pending_withdraws() -> list:
    return [w for w in _read(WITHDRAWS, []) if w.get("status") == "pending"]


def find_withdraw(key) -> Optional[dict]:
    """Ищет заявку по внутреннему id, wd_id (id из мини-аппа) или старому wd_short."""
    key = str(key)
    for w in _read(WITHDRAWS, []):
        if str(w.get("id")) == key or str(w.get("wd_id")) == key or str(w.get("wd_short")) == key:
            return w
    return None


def set_withdraw_status(key, status: str, by: int | None = None) -> Optional[dict]:
    """Меняет статус только у заявки в статусе pending (защита от двойного нажатия)."""
    key = str(key)
    items = _read(WITHDRAWS, [])
    for w in items:
        if str(w.get("id")) == key or str(w.get("wd_id")) == key or str(w.get("wd_short")) == key:
            if w.get("status") != "pending":
                return None
            w["status"] = status
            w["handled_by"] = by
            w["handled_at"] = int(time.time() * 1000)
            _write(WITHDRAWS, items)
            return w
    return None


# ---------------------------------------------------------------- withdraw window
# Вывод подарков открывается после пополнения на WD_UNLOCK_STARS и действует WD_WINDOW_DAYS дней.
DAY_MS = 24 * 60 * 60 * 1000


def wd_until(uid: int | str) -> int:
    u = get_user(uid) or {}
    try:
        return int(u.get("wd_until") or 0)
    except (TypeError, ValueError):
        return 0


def wd_active(uid: int | str, now_ms: int | None = None) -> bool:
    now_ms = now_ms or int(time.time() * 1000)
    return wd_until(uid) > now_ms


def extend_wd_window(uid: int, days: int = 7, now_ms: int | None = None) -> int:
    """Продлевает окно вывода. Если окно ещё активно — новая неделя добавляется к остатку."""
    now_ms = now_ms or int(time.time() * 1000)
    u = ensure_user(uid)
    start = max(now_ms, int(u.get("wd_until") or 0))
    u["wd_until"] = start + int(days) * DAY_MS
    save_user(u)
    return int(u["wd_until"])


# ---------------------------------------------------------------- promo codes
CODE_RE = re.compile(r"^[A-Z0-9_-]{3,24}$")


def norm_code(code: str) -> str:
    return (code or "").strip().upper()


def valid_code(code: str) -> bool:
    return bool(CODE_RE.match(norm_code(code)))


def _promos() -> dict:
    d = _read(PROMOS, {})
    return d if isinstance(d, dict) else {}


def get_promo(code: str) -> Optional[dict]:
    return _promos().get(norm_code(code))


def list_promos() -> list:
    items = list(_promos().values())
    items.sort(key=lambda p: p.get("created_at", 0), reverse=True)
    return items


def create_promo(code: str, reward: dict, max_uses: int, created_by: int) -> tuple[bool, str | dict]:
    """reward = {"ton": float, "free_case": bool, "gifts": [str]}"""
    code = norm_code(code)
    if not valid_code(code):
        return False, "bad_code"
    promos = _promos()
    if code in promos:
        return False, "exists"
    promo = {
        "code": code,
        "reward": {
            "ton": float(reward.get("ton") or 0),
            "free_case": bool(reward.get("free_case")),
            "gifts": list(reward.get("gifts") or []),
        },
        "max_uses": max(1, int(max_uses)),
        "used": 0,
        "used_by": [],
        "active": True,
        "created_by": int(created_by),
        "created_at": int(time.time() * 1000),
    }
    promos[code] = promo
    _write(PROMOS, promos)
    return True, promo


def redeem_promo(code: str, uid: int) -> tuple[str, Optional[dict]]:
    """Атомарно (бот однопоточный, без await внутри) списывает одну активацию.

    Статусы: ok | not_found | inactive | exhausted | already_used
    """
    code = norm_code(code)
    promos = _promos()
    p = promos.get(code)
    if not p:
        return "not_found", None
    if not p.get("active", True):
        return "inactive", p
    if int(uid) in [int(x) for x in p.get("used_by", [])]:
        return "already_used", p
    if int(p.get("used", 0)) >= int(p.get("max_uses", 1)):
        return "exhausted", p
    p["used"] = int(p.get("used", 0)) + 1
    p.setdefault("used_by", []).append(int(uid))
    _write(PROMOS, promos)
    return "ok", p


def undo_redeem(code: str, uid: int) -> None:
    """Откат активации, если награду выдать не удалось."""
    code = norm_code(code)
    promos = _promos()
    p = promos.get(code)
    if not p or int(uid) not in [int(x) for x in p.get("used_by", [])]:
        return
    p["used_by"] = [x for x in p["used_by"] if int(x) != int(uid)]
    p["used"] = max(0, int(p.get("used", 0)) - 1)
    _write(PROMOS, promos)


def set_promo_active(code: str, active: bool) -> bool:
    code = norm_code(code)
    promos = _promos()
    if code not in promos:
        return False
    promos[code]["active"] = bool(active)
    _write(PROMOS, promos)
    return True


def delete_promo(code: str) -> bool:
    code = norm_code(code)
    promos = _promos()
    if code not in promos:
        return False
    promos.pop(code)
    _write(PROMOS, promos)
    return True
