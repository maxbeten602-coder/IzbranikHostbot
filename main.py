"""Izbrannik Gift bot — no emojis, folder DB + optional Firebase mirror."""
from __future__ import annotations

import asyncio
import html
import base64
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from html import escape as esc
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)
from dotenv import load_dotenv

try:
    from bot import db as local_db
    from bot import gifts as gift_catalog
except ImportError:
    import db as local_db  # type: ignore
    import gifts as gift_catalog  # type: ignore

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nexven")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не светим ключи из URL в логах

BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or "").strip().strip("\"'").strip()  # убираем пробелы/кавычки, частая ошибка при вставке
if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    raise SystemExit(
        "\n\nBOT_TOKEN не задан или неверный (сейчас: "
        + ("пусто" if not BOT_TOKEN else f"{len(BOT_TOKEN)} символов") + ").\n"
        "Открой на хостинге раздел Variables / Переменные окружения и добавь переменную\n"
        "  BOT_TOKEN = 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   (токен целиком из @BotFather, без кавычек и пробелов)\n"
        "и перезапусти деплой.\n"
    )
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://maxbeten602-coder.github.io/IzbranikSites/")
_raw_admins = os.getenv("ADMIN_IDS", "8133917568")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]
OWNER_ID = int(os.getenv("OWNER_ID", "8133917568"))
FIREBASE_PROJECT = os.getenv("NX_FB_PROJECT", "nexora-aa0f3")  # старый env FIREBASE_PROJECT игнорируется (в нём был чужой проект); значения = config.js мини-аппа
FIREBASE_API_KEY = os.getenv("NX_FB_API_KEY", "AIzaSyAAw7lPLaShi_YOkvHHwzt17jiOFoHdb2Y")  # = apiKey в config.js
FIREBASE_DB = os.getenv("NX_FB_DB", "(default)")  # = databases[0] в config.js
FIREBASE_PREFIX = os.getenv("NX_FB_PREFIX", "nx_")  # = prefix в config.js мини-аппа
REF_PERCENT = float(os.getenv("REF_PERCENT", "2"))      # % от пополнения приглашённого

# Вывод подарков: пополнение от WD_UNLOCK_STARS открывает вывод на WD_WINDOW_DAYS дней
WD_UNLOCK_STARS = int(os.getenv("WD_UNLOCK_STARS", "100"))
WD_WINDOW_DAYS = int(os.getenv("WD_WINDOW_DAYS", "7"))
MIN_WITHDRAW_TON = float(os.getenv("MIN_WITHDRAW_TON", "3"))

# Курс пополнения: 1 Star = 0.0091 TON. Баланс в приложении считается в TON, оплата в Telegram Stars.
TON_PER_STAR = float(os.getenv("TON_PER_STAR", "0.0091"))
MIN_TOPUP_STARS = int(os.getenv("MIN_TOPUP_STARS", "10"))
TOPUP_PRESETS = [10, 50, 100, 250, 500]


def stars_to_ton(stars: int) -> float:
    return round(int(stars) * TON_PER_STAR, 4)


def fmt_ton(x: float) -> str:
    return f"{round(float(x), 4):.4f}".rstrip("0").rstrip(".")
BOT_USERNAME = os.getenv("BOT_USERNAME", "IzbrannikStar_bot")  # уточняется через get_me() при старте

# Обязательные подписки (бот должен быть админом в канале и чате)
CHANNEL_ID = os.getenv("CHANNEL_ID", "@IzbrannikGift")
CHAT_ID = os.getenv("CHAT_ID", "@IzbrannikGiftChat")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/IzbrannikGift")
CHAT_LINK = os.getenv("CHAT_LINK", "https://t.me/IzbrannikGiftChat")

DATA_DIR = Path(os.getenv("NX_DATA_DIR") or (Path(__file__).resolve().parent.parent / "db"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
REFUNDS_FILE = DATA_DIR / "refunds.json"
USERS_FILE = DATA_DIR / "bot_users.json"
REFS_FILE = DATA_DIR / "referrals.json"
BANS_FILE = DATA_DIR / "bans.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- Backend: Firebase (облако) или ПК (локальная Firestore в db/localfs.json) ----
LOCAL_PORT = int(os.getenv("NX_LOCAL_PORT", "8787"))
PUBLIC_URL = (os.getenv("PUBLIC_URL", "") or "").strip().rstrip("/")  # адрес туннеля (ngrok/cloudflared) — для самопроверки
BACKEND_FILE = DATA_DIR / "backend.json"
try:
    from bot import localfs
except ImportError:
    import localfs  # type: ignore
local_store = localfs.LocalFS(DATA_DIR / "localfs.json")


def backend_mode() -> str:
    m = load_json(BACKEND_FILE, {}).get("mode", "firebase")
    return m if m in ("pc", "firebase") else "firebase"


def set_backend_mode(m: str) -> None:
    save_json(BACKEND_FILE, {"mode": m, "ts": int(time.time() * 1000)})
    _ban_cache.clear()
    log.info("backend mode -> %s", m)


def fs_root() -> str:
    """Корень Firestore REST для текущего режима."""
    if backend_mode() == "pc":
        return f"http://127.0.0.1:{LOCAL_PORT}/v1/projects/{FIREBASE_PROJECT}/databases/{FIREBASE_DB}/documents"
    return f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/{FIREBASE_DB}/documents"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or uid == OWNER_ID


def staff_ids() -> list[int]:
    """Кому приходят заявки на вывод: только владелец и админы (или WITHDRAW_NOTIFY_IDS из .env)."""
    raw = os.getenv("WITHDRAW_NOTIFY_IDS", "")
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    if not ids:
        ids = [OWNER_ID] + ADMIN_IDS
    return list(dict.fromkeys(ids))


def fmt_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def fmt_num(x) -> str:
    x = float(x)
    return str(int(x)) if abs(x - round(x)) < 1e-9 else f"{x:.2f}".rstrip("0").rstrip(".")


# ---- Subscription checks ----
MEMBER_OK = {"member", "administrator", "creator"}


async def _is_member(chat_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in MEMBER_OK
    except Exception as e:
        log.warning("get_chat_member %s %s: %s", chat_id, user_id, e)
        return False


async def check_subscriptions(user_id: int) -> tuple[bool, bool]:
    """Returns (channel_ok, chat_ok). Admins always pass."""
    if is_admin(user_id):
        return True, True
    channel_ok = await _is_member(CHANNEL_ID, user_id)
    chat_ok = await _is_member(CHAT_ID, user_id)
    return channel_ok, chat_ok


async def is_fully_subscribed(user_id: int) -> bool:
    ch, ct = await check_subscriptions(user_id)
    return ch and ct


def subscribe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Канал Izbrannik Gift", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="Чат Izbrannik Gift", url=CHAT_LINK)],
        [InlineKeyboardButton(text="Проверить подписки", callback_data="check_subs")],
    ])


SUBSCRIBE_TEXT = (
    "<b>Подписка обязательна</b>\n\n"
    "Чтобы пользоваться ботом и мини-приложением, подпишись на канал и вступи в чат:\n\n"
    f"• Канал: {CHANNEL_LINK}\n"
    f"• Чат: {CHAT_LINK}\n\n"
    "После подписки нажми «Проверить подписки»."
)


ACCESS_DENIED_TEXT = (
    "<b>Доступ закрыт</b>\n\n"
    "Ты отписался от канала или вышел из чата.\n"
    "Подпишись снова, чтобы открыть доступ:\n\n"
    f"• Канал: {CHANNEL_LINK}\n"
    f"• Чат: {CHAT_LINK}\n\n"
    "Затем нажми «Проверить подписки»."
)


# ---- Firestore mirror (optional; works only if DB exists) ----
FS_MIRROR = os.getenv("NX_FS_MIRROR", "0") == "1"


async def fs_patch_user(user_id: int, fields: dict):
    if not FS_MIRROR:
        return 0, "mirror disabled"
    fs_fields = {}
    masks = []
    for k, v in fields.items():
        masks.append(k)
        if isinstance(v, bool):
            fs_fields[k] = {"booleanValue": v}
        elif isinstance(v, int):
            fs_fields[k] = {"integerValue": str(int(v))}
        elif isinstance(v, float):
            fs_fields[k] = {"doubleValue": float(v)}
        elif isinstance(v, str):
            fs_fields[k] = {"stringValue": str(v)}
        elif isinstance(v, list):
            fs_fields[k] = {
                "arrayValue": {"values": [{"stringValue": str(x)} for x in v]}
            }
        elif v is None:
            fs_fields[k] = {"nullValue": None}

    uid = str(user_id)
    mask_q = "&".join([f"updateMask.fieldPaths={m}" for m in masks])
    base = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
        f"/databases/premium/documents/users/{uid}"
    )
    body = {"fields": fs_fields}
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.patch(f"{base}?key={FIREBASE_API_KEY}&{mask_q}", json=body)
        if r.status_code in (200, 201):
            return r.status_code, r.text
        create_url = (
            f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
            f"/databases/premium/documents/users?documentId={uid}&key={FIREBASE_API_KEY}"
        )
        r2 = await client.post(create_url, json={"fields": fs_fields})
        return r2.status_code, r2.text


async def fs_get_user(user_id: int) -> dict:
    if not FS_MIRROR:
        return {}
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
        f"/databases/premium/documents/users/{user_id}?key={FIREBASE_API_KEY}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return {}
        fields = r.json().get("fields", {})
        out = {}
        for k, v in fields.items():
            if "integerValue" in v:
                out[k] = int(v["integerValue"])
            elif "doubleValue" in v:
                out[k] = float(v["doubleValue"])
            elif "stringValue" in v:
                out[k] = v["stringValue"]
            elif "booleanValue" in v:
                out[k] = v["booleanValue"]
        return out


def _num(x: float):
    x = round(float(x), 4)
    return int(x) if x == int(x) else x


async def fs_add_balance(user_id: int, amount: float, deposited: bool = False):
    user = await fs_get_user(user_id)
    bal = _num(float(user.get("balance", 0)) + float(amount))
    fields = {"balance": bal, "id": int(user_id), "last_active": int(time.time() * 1000)}
    if deposited:
        fields["total_deposited"] = _num(float(user.get("total_deposited", 0)) + float(amount))
    code, text = await fs_patch_user(user_id, fields)
    return code, bal


# ---- Rewards: bot -> mini-app ----
# Награда — маленький JSON {id, src, ton?, free?, gifts?, wdu?, code?}. Бот кладёт её в Firestore
# (grants/{uid}.rewards), мини-апп забирает сам. Те же данные лежат в кнопке «Открыть приложение»
# как запасной путь. Мини-апп хранит id применённых наград, поэтому дубль невозможен.
def make_reward(rid: str, src: str, ton: float = 0, free_case: bool = False,
                gifts: list | None = None, wdu: int = 0, code: str = "", ref: dict | None = None) -> dict:
    r: dict = {"id": str(rid), "src": src}
    if ref:
        r["ref"] = ref  # {"id": id друга, "name": ник} — мини-апп покажет его в списке рефералов
    if ton:
        t = float(ton)
        r["ton"] = int(t) if t == int(t) else round(t, 4)
    if free_case:
        r["free"] = 1
    if gifts:
        r["gifts"] = list(gifts)
    if wdu:
        r["wdu"] = int(wdu)
    if code:
        r["code"] = code
    return r


def reward_link(r: dict, uid: int | None = None) -> str:
    raw = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sep = "&" if "?" in WEBAPP_URL else "?"
    rs = refs_token(uid, limit=900) if uid else ""
    return f"{WEBAPP_URL}{sep}rw={token}" + (f"&rs={rs}" if rs else "") + f"&t={int(time.time())}"


def app_kb(r: dict | None = None, text: str = "Открыть приложение", uid: int | None = None) -> InlineKeyboardMarkup:
    url = reward_link(r, uid) if r else (webapp_url_for(uid) if uid else WEBAPP_URL)
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


_fs_lock = asyncio.Lock()


async def fs_push_grant(user_id: int, rewards: list, wd_until: int = 0) -> bool:
    """Добавляет награды в grants/{uid}. Оптимистичная блокировка по updateTime, 4 попытки."""
    base = f"{fs_root()}/{FIREBASE_PREFIX}rewards/{user_id}"
    async with _fs_lock:
        async with httpx.AsyncClient(timeout=12) as client:
            for attempt in range(4):
                r = await client.get(base, params={"key": FIREBASE_API_KEY})
                existing, update_time, cur_wdu = [], None, 0
                if r.status_code == 200:
                    doc = r.json()
                    update_time = doc.get("updateTime")
                    f = doc.get("fields", {})
                    try:
                        existing = json.loads((f.get("rewards") or {}).get("stringValue") or "[]")
                    except ValueError:
                        existing = []
                    cur_wdu = int((f.get("wd_until") or {}).get("integerValue") or 0)
                elif r.status_code == 404 and "does not exist" in r.text:
                    log.warning("Firestore database %s does not exist", FIREBASE_DB)
                    return False
                elif r.status_code != 404:
                    log.warning("grants GET %s %s", r.status_code, r.text[:200])
                    await asyncio.sleep(0.4)
                    continue
                have = {x.get("id") for x in existing if isinstance(x, dict)}
                existing += [x for x in rewards if x["id"] not in have]
                fields = {"rewards": {"stringValue": json.dumps(existing[-50:], ensure_ascii=False)}}
                if wd_until:
                    fields["wd_until"] = {"integerValue": str(max(cur_wdu, int(wd_until)))}
                params = [("key", FIREBASE_API_KEY)] + [("updateMask.fieldPaths", k) for k in fields]
                params.append(("currentDocument.updateTime", update_time) if update_time
                              else ("currentDocument.exists", "false"))
                r2 = await client.patch(base, params=params, json={"fields": fields})
                if r2.status_code in (200, 201):
                    return True
                log.info("grants PATCH retry %s: %s %s", attempt, r2.status_code, r2.text[:160])
                await asyncio.sleep(0.3)
    return False


async def deliver(user_id: int, rewards: list, wd_until: int = 0) -> bool:
    try:
        return await fs_push_grant(user_id, rewards, wd_until)
    except Exception as e:  # сеть/Firestore недоступны — останется кнопка в сообщении
        log.warning("deliver %s: %s", user_id, e)
        return False


async def admin_grant_ton(target: int, amount: int) -> tuple[bool, str]:
    """Выдача/снятие TON админом. Возвращает (доставлено в Firestore, id награды)."""
    local_db.ensure_user(target)
    local_db.add_balance(target, amount, deposited=False)
    try:
        await fs_add_balance(target, amount, deposited=False)
    except Exception as e:
        log.warning("fs mirror: %s", e)
    rw = make_reward(f"adm-{int(time.time() * 1000)}-{secrets.token_hex(3)}", "admin", ton=amount)
    ok = await deliver(target, [rw])
    try:
        await bot.send_message(
            target,
            f"Администратор изменил баланс: {amount:+d} TON.\n"
            f"Изменение применится само, когда откроешь приложение. Если нет — нажми кнопку.",
            reply_markup=app_kb(rw),
        )
    except Exception as e:
        log.warning("notify: %s", e)
    return ok, rw["id"]


# ---- Referrals ----
# t.me/bot?start=ref_<id пригласившего>. Друг сначала попадает в "pending"; рефералом он становится
# только после того, как подписался на канал и чат (проверка подписки пройдена) и бот показал ему
# кнопку «Открыть приложение». Тогда пригласившему приходит сообщение с ником, а в профиле мини-аппа
# появляется строка (через Firestore grants type='ref').
_refs_lock = asyncio.Lock()


def _refs_load() -> dict:
    d = load_json(REFS_FILE, {})
    d.setdefault("pending", {})     # invitee -> inviter
    d.setdefault("by_invitee", {})  # invitee -> inviter (подтверждённые)
    d.setdefault("inviters", {})    # inviter -> [{id, name, ts, earned}]
    return d


def user_label(u) -> str:
    """Ник для показа: @username, иначе имя."""
    un = getattr(u, "username", "") or ""
    return f"@{un}" if un else (getattr(u, "first_name", "") or f"id{u.id}")


def ref_register_pending(invitee: int, inviter_raw: str) -> str:
    """-> 'ok' | 'self' | 'already' | 'bad'"""
    if not inviter_raw.isdigit():
        return "bad"
    inviter = int(inviter_raw)
    if inviter == invitee:
        return "self"
    d = _refs_load()
    if str(invitee) in d["by_invitee"]:
        return "already"
    d["pending"][str(invitee)] = str(inviter)
    save_json(REFS_FILE, d)
    return "ok"


def refs_of(inviter: int) -> list:
    return _refs_load()["inviters"].get(str(inviter), [])


def refs_token(inviter: int, limit: int = 1100) -> str:
    """Компактный снимок списка рефералов для мини-аппа: [[id, name, earned], ...] в base64url."""
    rows = [[str(r["id"]), (r.get("name") or "")[:24], round(float(r.get("earned", 0)), 4)] for r in refs_of(inviter)]
    while rows:
        raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        tok = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        if len(tok) <= limit:
            return tok
        rows = rows[1:]  # самые старые отбрасываем
    return ""


def webapp_url_for(uid: int, extra: str = "") -> str:
    """Ссылка на мини-апп; если у игрока есть рефералы — со снимком списка (rs=...)."""
    sep = "&" if "?" in WEBAPP_URL else "?"
    url = WEBAPP_URL
    tok = refs_token(uid)
    parts = []
    if extra:
        parts.append(extra)
    if tok:
        parts.append(f"rs={tok}")
    if parts:
        url += sep + "&".join(parts) + f"&t={int(time.time())}"
    return url


async def fs_set_refs(inviter: int) -> None:
    """Дублирует снимок списка в Firestore ({prefix}rewards/{uid}.refs) — мини-апп забирает его сам."""
    url = f"{fs_root()}/{FIREBASE_PREFIX}rewards/{inviter}"
    rows = [{"id": str(r["id"]), "name": r.get("name") or "", "earned": round(float(r.get("earned", 0)), 4)}
            for r in refs_of(inviter)]
    body = {"fields": {"refs": {"stringValue": json.dumps(rows, ensure_ascii=False)}}}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.patch(url, params={"key": FIREBASE_API_KEY, "updateMask.fieldPaths": "refs"}, json=body)
        if r.status_code not in (200, 201):
            log.warning("fs_set_refs %s: %s %s", inviter, r.status_code, r.text[:160])
    except Exception as e:
        log.warning("fs_set_refs: %s", e)


async def ref_confirm(user: types.User) -> None:
    """Вызывать, когда пользователь прошёл проверку подписки."""
    uid = str(user.id)
    async with _refs_lock:
        d = _refs_load()
        inviter = d["pending"].pop(uid, None)
        if not inviter or uid in d["by_invitee"]:
            save_json(REFS_FILE, d)
            return
        name = user_label(user)
        d["by_invitee"][uid] = inviter
        d["inviters"].setdefault(inviter, []).append(
            {"id": user.id, "name": name, "ts": int(time.time() * 1000), "earned": 0})
        save_json(REFS_FILE, d)
    rw = make_reward(f"refnew-{inviter}-{user.id}", "ref", ref={"id": str(user.id), "name": name})
    await deliver(int(inviter), [rw])
    await fs_set_refs(int(inviter))
    log.info("referral confirmed: %s invited by %s", user.id, inviter)
    try:
        await bot.send_message(
            int(inviter),
            f"По вашей реферальной ссылке перешёл {name}\n"
            f"Вы будете получать {fmt_num(REF_PERCENT)}% с его пополнений.",
            reply_markup=app_kb(rw, uid=int(inviter)),
        )
    except Exception as e:
        log.warning("ref notify %s: %s", inviter, e)


async def ref_pay_bonus(payer: types.User, amount_ton: float) -> None:
    """Начисляет пригласившему REF_PERCENT% от пополнения друга."""
    d = _refs_load()
    inviter = d["by_invitee"].get(str(payer.id))
    if not inviter:
        return
    bonus = round(amount_ton * REF_PERCENT / 100, 4)
    if bonus <= 0:
        return
    inv = int(inviter)
    local_db.ensure_user(inv)
    local_db.add_balance(inv, bonus, deposited=False)
    try:
        await fs_add_balance(inv, bonus, deposited=False)
    except Exception as e:
        log.warning("fs mirror ref: %s", e)
    pname = user_label(payer)
    rw = make_reward(f"ref-{payer.id}-{int(time.time() * 1000)}", "referral", ton=bonus,
                     ref={"id": str(payer.id), "name": pname})
    await deliver(inv, [rw])
    name = user_label(payer)
    async with _refs_lock:
        d = _refs_load()
        for row in d["inviters"].get(inviter, []):
            if str(row.get("id")) == str(payer.id):
                row["earned"] = round(float(row.get("earned", 0)) + bonus, 4)
                name = row.get("name") or name
        save_json(REFS_FILE, d)
    await fs_set_refs(inv)
    try:
        await bot.send_message(
            inv,
            f"{name} пополнил баланс — вам начислено +{fmt_ton(bonus)} TON ({fmt_num(REF_PERCENT)}%).",
            reply_markup=app_kb(rw, uid=inv),
        )
    except Exception as e:
        log.warning("ref bonus notify: %s", e)


# ---- Referral menu / admin tools ----
def refs_text(uid: int) -> str:
    rows = refs_of(uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    total = sum(float(r.get("earned", 0)) for r in rows)
    head = f"<b>Рефералы</b>: {len(rows)} · заработано {fmt_ton(total)} TON ({fmt_num(REF_PERCENT)}% с пополнений)\n\n"
    if rows:
        body = "\n".join(f"{i}. {html.escape(str(r.get('name') or r['id']))} — +{fmt_ton(float(r.get('earned', 0)))} TON"
                         for i, r in enumerate(rows[-30:], 1)) + "\n\n"
    else:
        body = "Пока никого нет.\n\n"
    return head + body + f"Твоя ссылка:\n<code>{link}</code>"


async def send_refs(chat_id: int, uid: int):
    await bot.send_message(chat_id, refs_text(uid), parse_mode="HTML", reply_markup=app_kb(uid=uid, text="Открыть приложение"))


@dp.message(Command("refs"))
async def cmd_refs(message: types.Message):
    if not await is_fully_subscribed(message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_kb(), parse_mode="HTML")
        return
    await send_refs(message.chat.id, message.from_user.id)


@dp.callback_query(F.data == "refs_menu")
async def refs_menu_cb(cq: types.CallbackQuery):
    if not await require_subscription(cq):
        return
    await send_refs(cq.message.chat.id, cq.from_user.id)
    await cq.answer()


@dp.message(Command("refreset"))
async def cmd_refreset(message: types.Message):
    """Админ: /refreset <id друга> — снять привязку друга к пригласившему (для повторного теста)."""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /refreset <id друга>")
        return
    uid = parts[1]
    async with _refs_lock:
        d = _refs_load()
        inviter = d["by_invitee"].pop(uid, None)
        d["pending"].pop(uid, None)
        for k, rows in d["inviters"].items():
            d["inviters"][k] = [r for r in rows if str(r.get("id")) != uid]
        save_json(REFS_FILE, d)
    await message.answer(f"Привязка {uid} снята (был у {inviter or '—'}). Теперь он может перейти по новой ссылке.")
    if inviter:
        await fs_set_refs(int(inviter))


@dp.message(Command("fscheck"))
async def cmd_fscheck(message: types.Message):
    """Админ: проверка связи бот -> Firestore (пишет и читает тестовый документ в rewards)."""
    if not is_admin(message.from_user.id):
        return
    base = f"{fs_root()}/{FIREBASE_PREFIX}rewards/_fscheck"
    lines = [f"mode={backend_mode()}", f"project={FIREBASE_PROJECT}", f"db={FIREBASE_DB}", f"prefix={FIREBASE_PREFIX}"]
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            w = await client.patch(base, params={"key": FIREBASE_API_KEY, "updateMask.fieldPaths": "t"},
                                   json={"fields": {"t": {"integerValue": str(int(time.time()))}}})
            lines.append(f"WRITE {w.status_code}" + ("" if w.status_code in (200, 201) else f": {w.text[:300]}"))
            g = await client.get(base, params={"key": FIREBASE_API_KEY})
            lines.append(f"READ {g.status_code}")
    except Exception as e:
        lines.append(f"ERROR: {e}")
    lines.append("Рефералов в базе бота: " + str(sum(len(v) for v in _refs_load()["inviters"].values())))
    await message.answer("\n".join(lines))


# ---- Bans ----
# Бан хранится в db/bans.json (главный источник) и дублируется в Firestore {prefix}bans/{uid},
# откуда его читает мини-апп (и куда пишет бан из админки мини-аппа). Бот проверяет оба источника.
# until — время окончания в мс (0 = навсегда). Истёкший бан снимается автоматически.
_ban_cache: dict[int, tuple[float, dict | None]] = {}
BAN_CACHE_TTL = 45
TZ_HOURS = float(os.getenv("NX_TZ_HOURS", "3"))  # для отображения времени (Киев = 3)


def _bans_load() -> dict:
    return load_json(BANS_FILE, {})


def parse_duration(tok: str) -> int | None:
    """'30m' '2h' '7d' '2w' 'навсегда' -> секунды (0 = навсегда); None — это не срок."""
    t = tok.strip().lower()
    if t in ("perm", "forever", "навсегда", "вечно", "0"):
        return 0
    m = re.fullmatch(r"(\d+)\s*(m|min|мин|м|h|ч|d|д|w|н)?", t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "d"
    mult = {"m": 60, "min": 60, "мин": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400, "w": 604800, "н": 604800}[unit]
    return n * mult


def fmt_left(ms: int) -> str:
    sec = max(0, int(ms // 1000))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if mi or not parts:
        parts.append(f"{mi} мин")
    return " ".join(parts)


def fmt_local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000 + TZ_HOURS * 3600, tz=timezone.utc).strftime("%d.%m.%Y %H:%M")


def ban_period_text(until: int) -> str:
    if not until:
        return "Срок: навсегда"
    return f"Срок: до {fmt_local(until)} (осталось {fmt_left(until - int(time.time() * 1000))})"


async def fs_set_ban(uid: int, banned: bool, reason: str = "", by: int = 0, until: int = 0) -> bool:
    url = f"{fs_root()}/{FIREBASE_PREFIX}bans/{uid}"
    fields = {"banned": {"booleanValue": bool(banned)}, "reason": {"stringValue": reason or ""},
              "by": {"stringValue": str(by)}, "ts": {"integerValue": str(int(time.time() * 1000))},
              "until": {"integerValue": str(int(until or 0))}}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.patch(url, params=[("key", FIREBASE_API_KEY)] + [("updateMask.fieldPaths", k) for k in fields],
                                   json={"fields": fields})
        if r.status_code not in (200, 201):
            log.warning("fs_set_ban %s: %s %s", uid, r.status_code, r.text[:160])
            return False
        return True
    except Exception as e:
        log.warning("fs_set_ban: %s", e)
        return False


async def fs_get_ban(uid: int) -> dict | None:
    url = f"{fs_root()}/{FIREBASE_PREFIX}bans/{uid}"
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(url, params={"key": FIREBASE_API_KEY})
        if r.status_code != 200:
            return None
        f = r.json().get("fields", {})
        if (f.get("banned") or {}).get("booleanValue"):
            until = int((f.get("until") or {}).get("integerValue") or 0)
            if until and until <= time.time() * 1000:
                return None  # срок истёк
            return {"reason": (f.get("reason") or {}).get("stringValue", ""), "until": until}
    except Exception as e:
        log.debug("fs_get_ban: %s", e)
    return None


async def get_ban(uid: int) -> dict | None:
    """-> {'reason', 'until'} если забанен, иначе None. Админы и владелец не банятся."""
    if is_admin(uid):
        return None
    local = _bans_load().get(str(uid))
    if local:
        until = int(local.get("until") or 0)
        if until and until <= time.time() * 1000:
            await do_unban(uid, 0)
            return None
        return {"reason": local.get("reason", ""), "until": until}
    hit = _ban_cache.get(uid)
    if hit and time.time() - hit[0] < BAN_CACHE_TTL:
        return hit[1]
    res = await fs_get_ban(uid)
    _ban_cache[uid] = (time.time(), res)
    return res


async def do_ban(uid: int, reason: str, by: int, name: str = "", until: int = 0) -> None:
    d = _bans_load()
    d[str(uid)] = {"reason": reason, "ts": int(time.time() * 1000), "by": by, "name": name, "until": int(until or 0)}
    save_json(BANS_FILE, d)
    _ban_cache[uid] = (time.time(), {"reason": reason, "until": int(until or 0)})
    await fs_set_ban(uid, True, reason, by, until)


async def do_unban(uid: int, by: int) -> bool:
    d = _bans_load()
    was = d.pop(str(uid), None) is not None
    save_json(BANS_FILE, d)
    _ban_cache.pop(uid, None)
    ok = await fs_set_ban(uid, False, "", by, 0)
    return was or ok


def _resolve_target(token: str) -> tuple[int | None, str]:
    """'123456' или '@username' -> (id, имя)."""
    token = token.strip()
    if token.lstrip("-").isdigit():
        u = local_db.get_user(token) or {}
        return int(token), ("@" + u["username"]) if u.get("username") else u.get("first_name", "")
    u = local_db.find_by_username(token.lstrip("@"))
    if u:
        return int(u["id"]), ("@" + u["username"]) if u.get("username") else u.get("first_name", "")
    return None, ""


def _banned_text(reason: str, until: int = 0) -> str:
    t = "<b>Вы заблокированы</b>\n\n" + esc(ban_period_text(until))
    if reason:
        t += f"\nПричина: {esc(reason)}"
    return t + "\n\nПо вопросам — поддержка: https://t.me/nexvendropmananger"


from aiogram import BaseMiddleware  # noqa: E402


class BanMiddleware(BaseMiddleware):
    """Не пускает забаненных ни к одному хендлеру (сообщения, кнопки, оплата)."""
    async def __call__(self, handler, event: types.Update, data: dict):
        user = None
        kind = ""
        obj_ = None
        for k in ("message", "callback_query", "pre_checkout_query", "inline_query", "my_chat_member"):
            obj = getattr(event, k, None)
            if obj is not None and getattr(obj, "from_user", None):
                user, kind, obj_ = obj.from_user, k, obj
                break
        if user is None:
            return await handler(event, data)
        ban = await get_ban(user.id)
        if not ban:
            return await handler(event, data)
        try:
            if kind == "pre_checkout_query":
                await obj_.answer(ok=False, error_message="Вы заблокированы")
            elif kind == "callback_query":
                await obj_.answer("Вы заблокированы. " + ban_period_text(ban.get("until", 0)), show_alert=True)
            elif kind == "message":
                await obj_.answer(_banned_text(ban.get("reason", ""), ban.get("until", 0)), parse_mode="HTML")
        except Exception as e:
            log.debug("ban reply: %s", e)
        return None


dp.update.outer_middleware(BanMiddleware())


async def ban_expiry_loop():
    """Раз в минуту снимает истёкшие баны и сообщает игроку."""
    while True:
        try:
            now = int(time.time() * 1000)
            for uid, v in list(_bans_load().items()):
                until = int(v.get("until") or 0)
                if until and until <= now:
                    await do_unban(int(uid), 0)
                    try:
                        await bot.send_message(int(uid), "Срок блокировки истёк. Доступ к боту и приложению восстановлен.",
                                               reply_markup=app_kb())
                    except Exception:
                        pass
                    log.info("ban expired: %s", uid)
        except Exception as e:
            log.warning("ban_expiry_loop: %s", e)
        await asyncio.sleep(60)


BAN_USAGE = ("Использование: /ban <id или @username> [срок] [причина]\n"
             "Срок: 30m (минуты), 2h (часы), 7d (дни), 2w (недели) или «навсегда». Без срока — навсегда.\n"
             "Примеры:\n/ban 123456789 7d спам\n/ban @nick 2h оскорбления\n/ban 123456789 навсегда читерство")


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 2:
        await message.answer(BAN_USAGE)
        return
    uid, name = _resolve_target(parts[1])
    if uid is None:
        await message.answer("Не нашёл такого игрока. Укажи числовой Telegram ID.")
        return
    if is_admin(uid):
        await message.answer("Админа и владельца банить нельзя.")
        return
    secs, reason = 0, ""
    if len(parts) > 2:
        d = parse_duration(parts[2])
        if d is None:
            reason = " ".join(parts[2:]).strip()
        else:
            secs = d
            reason = parts[3].strip() if len(parts) > 3 else ""
    until = int(time.time() * 1000) + secs * 1000 if secs else 0
    await do_ban(uid, reason, message.from_user.id, name, until)
    try:
        await bot.send_message(uid, _banned_text(reason, until), parse_mode="HTML")
    except Exception:
        pass
    await message.answer(f"Забанен: {uid} {name}\n{ban_period_text(until)}\nПричина: {reason or '—'}\n"
                         f"Блокировка действует в боте и в мини-аппе.")


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unban <id или @username>")
        return
    uid, _ = _resolve_target(parts[1])
    if uid is None:
        await message.answer("Не нашёл такого игрока.")
        return
    await do_unban(uid, message.from_user.id)
    try:
        await bot.send_message(uid, "Блокировка снята. Доступ к боту и приложению восстановлен.", reply_markup=app_kb())
    except Exception:
        pass
    await message.answer(f"Разбанен: {uid}")


def bans_text() -> str:
    d = _bans_load()
    if not d:
        return "Список банов пуст.\n\n" + BAN_USAGE + "\n\n/unban <id или @username>"
    now = int(time.time() * 1000)
    rows = []
    for uid, v in list(d.items())[-40:]:
        until = int(v.get("until") or 0)
        left = "навсегда" if not until else f"ещё {fmt_left(until - now)}"
        rows.append(f"{uid} {v.get('name') or ''} — {left} — {v.get('reason') or 'без причины'}")
    return f"Забанено: {len(d)}\n\n" + "\n".join(rows) + "\n\n" + BAN_USAGE + "\n/unban <id или @username>"


@dp.message(Command("bans"))
async def cmd_bans(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer(bans_text())


@dp.callback_query(F.data == "adm_bans")
async def adm_bans_cb(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await cq.message.answer(bans_text())
    await cq.answer()


# ---- Консоль выбора базы (ПК / Firebase) ----
def fb_root() -> str:
    return f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/{FIREBASE_DB}/documents"


async def console_fb_test() -> dict:
    url = f"{fb_root()}/{FIREBASE_PREFIX}rewards/_fscheck"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            w = await client.patch(url, params={"key": FIREBASE_API_KEY, "updateMask.fieldPaths": "t"},
                                   json={"fields": {"t": {"integerValue": str(int(time.time()))}}})
        if w.status_code in (200, 201):
            return {"ok": True, "text": f"Firebase работает: запись прошла (HTTP {w.status_code}).\nПроект {FIREBASE_PROJECT}, база {FIREBASE_DB}."}
        hint = ""
        if w.status_code == 429:
            hint = "\nЛимит бесплатной квоты на сегодня исчерпан — сбросится около 10:00 по Киеву. Можно переключиться на «ПК»."
        elif w.status_code == 404:
            hint = "\nБаза данных не создана в этом проекте (Firebase Console → Firestore Database → Create database)."
        elif w.status_code == 403:
            hint = "\nДоступ запрещён: проверь правила Firestore и ограничения API-ключа."
        return {"ok": False, "text": f"Firebase ответил HTTP {w.status_code}.{hint}\n\n{w.text[:400]}"}
    except Exception as e:
        return {"ok": False, "text": f"Нет связи с Firebase: {e}"}


async def console_fb_import() -> dict:
    """Копирует данные из Firebase в базу ПК (постранично). Может упереться в квоту — тогда сообщает об этом."""
    names = ["players", "saves", "bans", "promos", "promo_uses", "rewards", "grants"]
    total, lines = 0, []
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            for n in names:
                coll, token, cnt = FIREBASE_PREFIX + n, "", 0
                while True:
                    params = {"key": FIREBASE_API_KEY, "pageSize": "300"}
                    if token:
                        params["pageToken"] = token
                    r = await client.get(f"{fb_root()}/{coll}", params=params)
                    if r.status_code == 429:
                        lines.append(f"{n}: лимит квоты Firebase исчерпан (429) — импорт остановлен")
                        return {"ok": False, "text": "\n".join(lines) + f"\nИмпортировано записей: {total}"}
                    if r.status_code != 200:
                        lines.append(f"{n}: HTTP {r.status_code}")
                        break
                    j = r.json()
                    for d in j.get("documents", []):
                        local_store.put_raw(coll, d["name"].rsplit("/", 1)[-1], d.get("fields", {}))
                        cnt += 1
                    token = j.get("nextPageToken", "")
                    if not token:
                        break
                total += cnt
                lines.append(f"{n}: {cnt}")
        local_store.flush()
        return {"ok": True, "text": "Импорт завершён.\n" + "\n".join(lines) + f"\nВсего записей: {total}"}
    except Exception as e:
        return {"ok": False, "text": f"Ошибка импорта: {e}\n" + "\n".join(lines)}


async def console_ext_test() -> dict:
    if not PUBLIC_URL:
        return {"ok": False, "text": "PUBLIC_URL не задан в .env. Запусти туннель (start_tunnel.bat) и впиши его адрес, например:\nPUBLIC_URL=https://твой-домен.ngrok-free.app"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(f"{PUBLIC_URL}/mode", headers={"ngrok-skip-browser-warning": "1"})
        j = r.json()
        return {"ok": True, "text": f"Сервер доступен из интернета: {PUBLIC_URL}\nРежим на сервере: {j.get('mode')}"}
    except Exception as e:
        return {"ok": False, "text": f"Не достучался до {PUBLIC_URL}: {e}\nПроверь, что запущен start_tunnel.bat и бот."}


async def console_status() -> dict:
    return {"mode": backend_mode(), "port": LOCAL_PORT, "publicUrl": PUBLIC_URL, "pcApiHint": bool(PUBLIC_URL),
            "fbProject": FIREBASE_PROJECT, "fbDb": FIREBASE_DB, "counts": local_store.counts(), "bans": len(_bans_load())}


async def start_local_server():
    ctx = {"get_mode": backend_mode, "set_mode": set_backend_mode, "status": console_status,
           "fb_test": console_fb_test, "fb_import": console_fb_import, "ext_test": console_ext_test}
    app = localfs.build_app(local_store, ctx)
    await localfs.start_server(app, "127.0.0.1", LOCAL_PORT)
    log.info("Local DB server: http://127.0.0.1:%s  | Console: http://127.0.0.1:%s/console  | mode=%s",
             LOCAL_PORT, LOCAL_PORT, backend_mode())


def db_kb() -> InlineKeyboardMarkup:
    cur = backend_mode()
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=("● " if cur == "firebase" else "") + "Firebase", callback_data="db_set:firebase"),
        InlineKeyboardButton(text=("● " if cur == "pc" else "") + "ПК", callback_data="db_set:pc"),
    ]])


def db_text() -> str:
    cur = "ПК (локально)" if backend_mode() == "pc" else "Firebase (облако)"
    return f"База данных сейчас: {cur}\nВыбери, куда писать лидерборд, онлайн, баны и награды. Приложение подхватит смену в течение минуты."


@dp.message(Command("db"))
async def cmd_db(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer(db_text(), reply_markup=db_kb())


@dp.callback_query(F.data == "adm_db")
async def cb_adm_db(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await cq.message.answer(db_text(), reply_markup=db_kb())
    await cq.answer()


@dp.callback_query(F.data.startswith("db_set:"))
async def cb_db_set(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    set_backend_mode(cq.data.split(":", 1)[1])
    try:
        await cq.message.edit_text(db_text(), reply_markup=db_kb())
    except Exception:
        pass
    await cq.answer("Готово")



# ---- Keyboards ----
def main_menu_kb(uid: int):
    rows = [
        [InlineKeyboardButton(text="Open app", web_app=WebAppInfo(url=webapp_url_for(uid)))],
        [InlineKeyboardButton(text="Пополнить баланс", callback_data="pay_menu")],
        [InlineKeyboardButton(text="Рефералы", callback_data="refs_menu")],
        [
            InlineKeyboardButton(text="Промокод", callback_data="promo_enter"),
            InlineKeyboardButton(text="Вывод подарков", callback_data="wd_info"),
        ],
        [InlineKeyboardButton(text="Privacy policy", callback_data="privacy")],
        [InlineKeyboardButton(text="Refund request", callback_data="refund")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Give TON", callback_data="adm_give_ton")],
        [InlineKeyboardButton(text="Give free case", callback_data="adm_give_case")],
        [InlineKeyboardButton(text="Промокоды", callback_data="adm_promos")],
        [InlineKeyboardButton(text="Баны", callback_data="adm_bans")],
        [InlineKeyboardButton(text="База данных: ПК / Firebase", callback_data="adm_db")],
        [InlineKeyboardButton(text="Withdraw requests", callback_data="adm_withdraws")],
        [InlineKeyboardButton(text="Refund requests", callback_data="adm_refunds")],
        [InlineKeyboardButton(text="Back", callback_data="back_menu")],
    ])


PRIVACY = (
    "<b>Privacy policy</b>\n\n"
    "We store Telegram ID, username, balance and inventory "
    "to run the mini-app. Payments go through Telegram TON. "
    "Contact support via this bot for refunds."
)


class Form(StatesGroup):
    refund_reason = State()
    adm_ton_id = State()
    adm_ton_amount = State()
    adm_case_id = State()
    promo_enter = State()
    topup_custom = State()
    promo_new_code = State()
    promo_ton = State()
    promo_uses = State()


# ---- Handlers ----
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    local_db.ensure_user(
        message.from_user.id,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username or "",
    )
    users = load_json(USERS_FILE, {})
    uid = str(message.from_user.id)
    users[uid] = {
        "id": message.from_user.id,
        "first_name": message.from_user.first_name or "",
        "username": message.from_user.username or "",
    }
    save_json(USERS_FILE, users)

    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        st = ref_register_pending(message.from_user.id, args[1][len("ref_"):])
        if st == "self":
            await message.answer("Это твоя собственная реферальная ссылка — отправь её другу.")
        elif st == "already":
            await message.answer("Ты уже приглашён другим игроком — эта ссылка не засчитана (реферал закрепляется за первым).")

    # Проверка подписки (обязательна для всех, кроме админов)
    if not await is_fully_subscribed(message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_kb(), parse_mode="HTML")
        return

    await ref_confirm(message.from_user)

    if len(args) > 1 and args[1].startswith("pay_"):
        try:
            amount = int(args[1].replace("pay_", ""))
            if amount >= MIN_TOPUP_STARS:
                await send_invoice(message, amount)
                return
        except ValueError:
            pass

    # Promo link: t.me/bot?start=promo_CODE  (и вкладка «Промо» в мини-аппе)
    if len(args) > 1 and args[1].startswith("promo_"):
        await state.clear()
        await process_promo(message.chat.id, message.from_user.id, args[1][len("promo_"):])
        return

    # Admin: открыть управление промокодами из мини-аппа
    if len(args) > 1 and args[1] == "adm_promo" and is_admin(message.from_user.id):
        await message.answer("Промокоды:", reply_markup=promos_menu_kb())
        return

    # Admin give from mini-app: start=give_{targetId}_{amount}
    if len(args) > 1 and args[1].startswith("give_"):
        if not is_admin(message.from_user.id):
            await message.answer("Only admin can grant TON.", reply_markup=main_menu_kb(message.from_user.id))
            return
        parts = args[1].split("_")
        try:
            target = int(parts[1])
            amount_i = int(round(float(parts[2]))) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            await message.answer("Bad give payload. Use Admin panel in bot.")
            return
        if amount_i == 0:
            await message.answer("Amount must be non-zero integer TON for bot grant.")
            return
        delivered, _ = await admin_grant_ton(target, amount_i)
        await message.answer(
            f"Granted {amount_i:+d} TON to {target}.\n"
            + ("Sent to the app automatically." if delivered else "Firestore unavailable: the player has a button in DM."),
            reply_markup=main_menu_kb(message.from_user.id),
        )
        return

    # Withdraw request from mini-app: start=wd_{uid}_{slug}_{value}_{id}  (старый формат: wd_{uid}_{value}_{short})
    if len(args) > 1 and args[1].startswith("wd_"):
        await process_withdraw_request(message, args[1])
        return

    await message.answer(
        "Izbrannik Gift\n\n"
        "Open the app to play cases and games.\n"
        "Balance is saved on your Telegram account (CloudStorage) "
        "so phone and PC stay in sync.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@dp.callback_query(F.data == "check_subs")
async def cb_check_subs(cq: types.CallbackQuery):
    channel_ok, chat_ok = await check_subscriptions(cq.from_user.id)
    if channel_ok and chat_ok:
        await ref_confirm(cq.from_user)
        try:
            await cq.message.edit_text(
                "Подписки подтверждены. Доступ открыт.",
                reply_markup=main_menu_kb(cq.from_user.id),
            )
        except Exception:
            await cq.message.answer(
                "Подписки подтверждены. Доступ открыт.",
                reply_markup=main_menu_kb(cq.from_user.id),
            )
        await cq.answer("OK")
        return

    missing = []
    if not channel_ok:
        missing.append("канал")
    if not chat_ok:
        missing.append("чат")
    detail = " и ".join(missing)
    try:
        await cq.message.edit_text(
            f"Ещё нет подписки на {detail}.\n\n{SUBSCRIBE_TEXT}",
            reply_markup=subscribe_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await cq.message.answer(
            f"Ещё нет подписки на {detail}.\n\n{SUBSCRIBE_TEXT}",
            reply_markup=subscribe_kb(),
            parse_mode="HTML",
        )
    await cq.answer(f"Нет подписки: {detail}", show_alert=True)


async def send_topup_invoice(chat_id: int, user_id: int, stars: int):
    """Счёт в Telegram Stars. Игрок платит stars, получает stars * TON_PER_STAR TON."""
    ton = stars_to_ton(stars)
    bonus = ""
    if stars >= WD_UNLOCK_STARS:
        bonus = f"\nПлюс вывод подарков на {WD_WINDOW_DAYS} дней."
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Пополнение: {fmt_ton(ton)} TON",
        description=f"{stars} Stars = {fmt_ton(ton)} TON (1 Star = {TON_PER_STAR:g} TON).{bonus}",
        payload=f"topup_{user_id}_{stars}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{stars} Stars", amount=stars)],
    )


async def send_invoice(message: types.Message, amount: int):
    await send_topup_invoice(message.chat.id, message.from_user.id, amount)


async def require_subscription(cq: types.CallbackQuery) -> bool:
    """Если не подписан — показывает экран подписки и возвращает False."""
    if await is_fully_subscribed(cq.from_user.id):
        return True
    try:
        await cq.message.edit_text(
            ACCESS_DENIED_TEXT,
            reply_markup=subscribe_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await cq.message.answer(
            ACCESS_DENIED_TEXT,
            reply_markup=subscribe_kb(),
            parse_mode="HTML",
        )
    await cq.answer("Подписка обязательна", show_alert=True)
    return False


def topup_text() -> str:
    return (
        "<b>Пополнение баланса</b>\n\n"
        f"Курс: 1 Star = {TON_PER_STAR:g} TON\n"
        f"Минимум: {MIN_TOPUP_STARS} Stars\n\n"
        f"Пополнение от {WD_UNLOCK_STARS} Stars одним платежом открывает вывод подарков на {WD_WINDOW_DAYS} дней.\n\n"
        "Выбери, сколько Stars оплатить:"
    )


def topup_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for st in TOPUP_PRESETS:
        row.append(InlineKeyboardButton(text=f"{st} Stars = {fmt_ton(stars_to_ton(st))} TON", callback_data=f"pay:{st}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Своя сумма", callback_data="pay_custom")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "pay_menu")
async def pay_menu(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not await require_subscription(cq):
        return
    await cq.message.edit_text(topup_text(), reply_markup=topup_kb(), parse_mode="HTML")
    await cq.answer()


@dp.callback_query(F.data.startswith("pay:"))
async def pay_amount(cq: types.CallbackQuery):
    if not await require_subscription(cq):
        return
    try:
        stars = int(cq.data.split(":")[1])
    except (IndexError, ValueError):
        await cq.answer()
        return
    if stars < MIN_TOPUP_STARS:
        await cq.answer(f"Минимум {MIN_TOPUP_STARS} Stars", show_alert=True)
        return
    await cq.answer()
    await send_topup_invoice(cq.message.chat.id, cq.from_user.id, stars)


@dp.callback_query(F.data == "pay_custom")
async def pay_custom(cq: types.CallbackQuery, state: FSMContext):
    if not await require_subscription(cq):
        return
    await state.set_state(Form.topup_custom)
    await cq.message.edit_text(
        f"Напиши числом, сколько Stars оплатить (минимум {MIN_TOPUP_STARS}).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="pay_menu")]]),
    )
    await cq.answer()


@dp.message(Form.topup_custom)
async def pay_custom_msg(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().replace(" ", "")
    if not txt.isdigit() or not (MIN_TOPUP_STARS <= int(txt) <= 100000):
        await message.answer(f"Нужно целое число от {MIN_TOPUP_STARS} до 100000. Попробуй ещё раз.")
        return
    await state.clear()
    await send_topup_invoice(message.chat.id, message.from_user.id, int(txt))


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    parts = payload.split("_")
    try:
        user_id = int(parts[1])
        stars = int(parts[2])
    except (IndexError, ValueError):
        user_id = message.from_user.id
        stars = int(sp.total_amount)
    if sp.currency == "XTR":
        stars = int(sp.total_amount)  # сколько Stars реально оплачено — доверяем Telegram, а не payload
    amount = stars_to_ton(stars)      # TON на баланс: stars * 0.0091

    local_db.add_balance(user_id, amount, deposited=True)
    try:
        await fs_add_balance(user_id, amount, deposited=True)
    except Exception as e:
        log.warning("fs mirror pay: %s", e)

    # Пополнение от WD_UNLOCK_STARS Stars одним платежом открывает вывод на WD_WINDOW_DAYS дней
    until = 0
    if sp.currency == "XTR" and int(sp.total_amount) >= WD_UNLOCK_STARS:
        until = local_db.extend_wd_window(user_id, WD_WINDOW_DAYS)

    rw = make_reward(f"dep-{sp.telegram_payment_charge_id or secrets.token_hex(6)}", "deposit", ton=amount, wdu=until)
    delivered = await deliver(user_id, [rw], wd_until=until)

    try:
        await ref_pay_bonus(message.from_user, amount)
    except Exception as e:
        log.warning("ref bonus: %s", e)

    text = f"Оплата прошла: {stars} Stars, на баланс +{fmt_ton(amount)} TON."
    if until:
        text += f"\nВывод подарков открыт до {fmt_dt(until)}."
    else:
        text += (f"\nЧтобы открыть вывод подарков, пополни одним платежом от {WD_UNLOCK_STARS} Stars "
                 f"(откроется на {WD_WINDOW_DAYS} дней).")
    text += "\nБаланс обновится сам при открытии приложения." if delivered else "\nНажми кнопку, чтобы получить баланс в приложении."
    await message.answer(text, reply_markup=app_kb(rw))


@dp.callback_query(F.data == "back_menu")
async def back_menu(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not await require_subscription(cq):
        return
    await cq.message.edit_text("Menu:", reply_markup=main_menu_kb(cq.from_user.id))
    await cq.answer()


@dp.callback_query(F.data == "privacy")
async def privacy(cq: types.CallbackQuery):
    if not await require_subscription(cq):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back", callback_data="back_menu")]
    ])
    await cq.message.edit_text(PRIVACY, reply_markup=kb, parse_mode="HTML")
    await cq.answer()


@dp.callback_query(F.data == "refund")
async def refund_start(cq: types.CallbackQuery, state: FSMContext):
    if not await require_subscription(cq):
        return
    await state.set_state(Form.refund_reason)
    await cq.message.edit_text(
        "Describe why you need a refund:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="back_menu")]
        ]),
    )
    await cq.answer()


@dp.message(Form.refund_reason)
async def refund_reason(message: types.Message, state: FSMContext):
    reason = (message.text or "")[:500]
    reqs = load_json(REFUNDS_FILE, [])
    req = {
        "id": len(reqs) + 1,
        "uid": message.from_user.id,
        "username": message.from_user.username or "",
        "reason": reason,
        "status": "pending",
        "ts": int(time.time()),
    }
    reqs.append(req)
    save_json(REFUNDS_FILE, reqs)
    await state.clear()
    await message.answer(f"Request #{req['id']} sent.", reply_markup=main_menu_kb(message.from_user.id))
    text = (
        f"Refund #{req['id']}\n"
        f"User: {req['uid']} @{req['username']}\n"
        f"{reason}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"OK {req['id']}", callback_data=f"ref_ok:{req['id']}"),
        InlineKeyboardButton(text=f"NO {req['id']}", callback_data=f"ref_no:{req['id']}"),
    ]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("ref_ok:"))
async def ref_ok(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    rid = int(cq.data.split(":")[1])
    reqs = load_json(REFUNDS_FILE, [])
    for r in reqs:
        if r["id"] == rid and r["status"] == "pending":
            r["status"] = "approved"
            # restore spent as negative balance allowance — give back last known spent chunk simply notify
            try:
                await bot.send_message(
                    r["uid"],
                    "Refund approved. Contact admin for TON return if needed.",
                )
            except Exception:
                pass
            break
    save_json(REFUNDS_FILE, reqs)
    await cq.answer("Approved")
    await cq.message.edit_text(cq.message.text + "\n\nApproved.")


@dp.callback_query(F.data.startswith("ref_no:"))
async def ref_no(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    rid = int(cq.data.split(":")[1])
    reqs = load_json(REFUNDS_FILE, [])
    for r in reqs:
        if r["id"] == rid:
            r["status"] = "rejected"
            break
    save_json(REFUNDS_FILE, reqs)
    await cq.answer("Rejected")
    await cq.message.edit_text(cq.message.text + "\n\nRejected.")


# ---- Admin ----
@dp.callback_query(F.data == "admin")
async def admin_panel(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await state.clear()
    await cq.message.edit_text("Admin panel:", reply_markup=admin_kb())
    await cq.answer()


@dp.callback_query(F.data == "adm_give_ton")
async def adm_ton_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(Form.adm_ton_id)
    await cq.message.edit_text(
        "Telegram ID of user:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="admin")]
        ]),
    )
    await cq.answer()


@dp.message(Form.adm_ton_id)
async def adm_ton_id(message: types.Message, state: FSMContext):
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("Need numeric ID")
        return
    await state.update_data(target=target)
    await state.set_state(Form.adm_ton_amount)
    await message.answer("How many TON? (minus = take)")


@dp.message(Form.adm_ton_amount)
async def adm_ton_amount(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = int(data["target"])
    try:
        amount = int((message.text or "").strip())
    except ValueError:
        await message.answer("Need a number")
        return
    await state.clear()
    delivered, _ = await admin_grant_ton(target, amount)
    await message.answer(
        f"Done -> ID {target}: {'+' if amount >= 0 else ''}{amount} TON\n"
        + ("Sent to the app automatically." if delivered else "Firestore unavailable: the player got a button in DM."),
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "adm_give_case")
async def adm_case_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(Form.adm_case_id)
    await cq.message.edit_text(
        "User ID for free case reset:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="admin")]
        ]),
    )
    await cq.answer()


@dp.message(Form.adm_case_id)
async def adm_case_id(message: types.Message, state: FSMContext):
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("Need numeric ID")
        return
    await state.clear()
    u = local_db.ensure_user(target)
    u["last_free"] = 0
    local_db.save_user(u)
    try:
        await fs_patch_user(target, {"last_free": 0, "id": target})
    except Exception:
        pass
    free_url = f"{WEBAPP_URL}?free=1&t={int(time.time())}"
    try:
        await bot.send_message(
            target,
            "Free case is ready.\nPress the button to claim.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Claim free case", web_app=WebAppInfo(url=free_url))]
            ]),
        )
    except Exception:
        pass
    await message.answer(
        f"Free case reset for {target}\nFile: db/users/{target}.json",
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "adm_refunds")
async def adm_refunds(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    reqs = [r for r in load_json(REFUNDS_FILE, []) if r.get("status") == "pending"]
    if not reqs:
        await cq.message.edit_text("No refund requests.", reply_markup=admin_kb())
        await cq.answer()
        return
    buttons = []
    for r in reqs[:10]:
        buttons.append([
            InlineKeyboardButton(text=f"OK{r['id']}", callback_data=f"ref_ok:{r['id']}"),
            InlineKeyboardButton(text=f"NO{r['id']}", callback_data=f"ref_no:{r['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="admin")])
    text = "\n".join([f"#{r['id']} {r['uid']} @{r.get('username','')}: {r.get('reason','')[:40]}" for r in reqs[:10]])
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await cq.answer()


# ================================================================== PROMO CODES
def describe_reward(rw: dict) -> str:
    parts = []
    if rw.get("ton"):
        parts.append(f"{fmt_num(rw['ton'])} TON")
    parts += list(rw.get("gifts") or [])
    if rw.get("free_case"):
        parts.append("бесплатный кейс")
    return " + ".join(parts) or "—"


PROMO_ERRORS = {
    "not_found": "Такого промокода нет. Проверь написание.",
    "inactive": "Этот промокод отключён.",
    "exhausted": "Лимит активаций этого промокода исчерпан.",
    "already_used": "Ты уже активировал этот промокод.",
}


async def process_promo(chat_id: int, uid: int, code: str, check_sub: bool = False):
    """Активация промокода: проверка, списание активации, доставка награды в мини-апп."""
    if check_sub and not await is_fully_subscribed(uid):
        await bot.send_message(chat_id, SUBSCRIBE_TEXT, reply_markup=subscribe_kb(), parse_mode="HTML")
        return
    code = local_db.norm_code(code)
    status, promo = local_db.redeem_promo(code, uid)
    if status != "ok":
        await bot.send_message(chat_id, PROMO_ERRORS[status], reply_markup=main_menu_kb(uid))
        return
    rwd = promo["reward"]
    rw = make_reward(
        f"promo-{code}-{uid}", "promo",
        ton=rwd.get("ton", 0), free_case=bool(rwd.get("free_case")), gifts=rwd.get("gifts"), code=code,
    )
    delivered = await deliver(uid, [rw])
    text = f"Промокод {code} активирован.\nНаграда: {describe_reward(rwd)}.\n"
    text += ("Она придёт в приложение сама, когда ты его откроешь. Если не появилась — нажми кнопку."
             if delivered else "Нажми кнопку, чтобы получить награду в приложении.")
    await bot.send_message(chat_id, text, reply_markup=app_kb(rw, "Получить в приложении"))


@dp.callback_query(F.data == "promo_enter")
async def promo_enter_cb(cq: types.CallbackQuery, state: FSMContext):
    if not await require_subscription(cq):
        return
    await state.set_state(Form.promo_enter)
    await cq.message.edit_text(
        "Введи промокод одним сообщением:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="back_menu")]
        ]),
    )
    await cq.answer()


@dp.message(Form.promo_enter)
async def promo_enter_msg(message: types.Message, state: FSMContext):
    await state.clear()
    await process_promo(message.chat.id, message.from_user.id, message.text or "", check_sub=True)


@dp.message(Command("promo"))
async def cmd_promo(message: types.Message, state: FSMContext):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await state.set_state(Form.promo_enter)
        await message.answer("Введи промокод одним сообщением:")
        return
    await process_promo(message.chat.id, message.from_user.id, parts[1], check_sub=True)


# ---- admin: создание и управление промокодами ----
def promos_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Создать промокод", callback_data="pr_new")],
        [InlineKeyboardButton(text="Список промокодов", callback_data="pr_list")],
        [InlineKeyboardButton(text="Back", callback_data="admin")],
    ])


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_promos")]])


@dp.callback_query(F.data == "adm_promos")
async def adm_promos(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await state.clear()
    await cq.message.edit_text("Промокоды:", reply_markup=promos_menu_kb())
    await cq.answer()


@dp.callback_query(F.data == "pr_new")
async def pr_new(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await state.clear()
    await state.set_state(Form.promo_new_code)
    await cq.message.edit_text(
        "Введи код промокода: латиница, цифры, - и _, от 3 до 24 символов.\nИли нажми «Сгенерировать».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Сгенерировать", callback_data="pr_gen")],
            [InlineKeyboardButton(text="Отмена", callback_data="adm_promos")],
        ]),
    )
    await cq.answer()


def _reward_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="TON на баланс", callback_data="pr_t:ton")],
        [InlineKeyboardButton(text="Подарок", callback_data="pr_t:gift")],
        [InlineKeyboardButton(text="Бесплатный кейс", callback_data="pr_t:free")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm_promos")],
    ])


@dp.callback_query(F.data == "pr_gen")
async def pr_gen(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = ""
    for _ in range(20):
        code = "NX" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not local_db.get_promo(code):
            break
    await state.update_data(code=code)
    await state.set_state(None)
    await cq.message.edit_text(f"Код: {code}\n\nЧто получит игрок?", reply_markup=_reward_type_kb())
    await cq.answer()


@dp.message(Form.promo_new_code)
async def pr_code_msg(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    code = local_db.norm_code(message.text or "")
    if not local_db.valid_code(code):
        await message.answer("Код должен состоять из латиницы, цифр, - и _, длина 3-24. Попробуй ещё раз.",
                             reply_markup=_cancel_kb())
        return
    if local_db.get_promo(code):
        await message.answer("Такой промокод уже есть. Введи другой.", reply_markup=_cancel_kb())
        return
    await state.update_data(code=code)
    await state.set_state(None)
    await message.answer(f"Код: {code}\n\nЧто получит игрок?", reply_markup=_reward_type_kb())


async def _ask_uses(chat_id: int, state: FSMContext):
    await state.set_state(Form.promo_uses)
    await bot.send_message(
        chat_id,
        "Сколько раз можно активировать промокод? Выбери или введи число (1 игрок = 1 активация).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=str(n), callback_data=f"pr_u:{n}") for n in (1, 10, 100, 1000)],
            [InlineKeyboardButton(text="Отмена", callback_data="adm_promos")],
        ]),
    )


@dp.callback_query(F.data.startswith("pr_t:"))
async def pr_type(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    if not (await state.get_data()).get("code"):
        await cq.answer("Сессия сброшена, начни заново", show_alert=True)
        return
    kind = cq.data.split(":")[1]
    await cq.answer()
    if kind == "ton":
        await state.set_state(Form.promo_ton)
        await cq.message.edit_text("Сколько TON выдавать за активацию? (например 5 или 0.5)", reply_markup=_cancel_kb())
    elif kind == "gift":
        await cq.message.edit_text("Выбери подарок:", reply_markup=gift_page_kb(0))
    else:
        await state.update_data(reward={"free_case": True})
        await _ask_uses(cq.message.chat.id, state)


GIFT_PAGE = 8


def gift_page_kb(page: int) -> InlineKeyboardMarkup:
    names = gift_catalog.NAMES
    pages = (len(names) + GIFT_PAGE - 1) // GIFT_PAGE
    page = max(0, min(page, pages - 1))
    chunk = list(enumerate(names))[page * GIFT_PAGE:(page + 1) * GIFT_PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        rows.append([
            InlineKeyboardButton(text=f"{n} · {fmt_num(gift_catalog.GIFTS[n][0])}", callback_data=f"pr_g:{idx}")
            for idx, n in chunk[i:i + 2]
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="<", callback_data=f"pr_gp:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="pr_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text=">", callback_data=f"pr_gp:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="adm_promos")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "pr_noop")
async def pr_noop(cq: types.CallbackQuery):
    await cq.answer()


@dp.callback_query(F.data.startswith("pr_gp:"))
async def pr_gift_page(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await cq.message.edit_reply_markup(reply_markup=gift_page_kb(int(cq.data.split(":")[1])))
    await cq.answer()


@dp.callback_query(F.data.startswith("pr_g:"))
async def pr_gift_pick(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    try:
        name = gift_catalog.NAMES[int(cq.data.split(":")[1])]
    except (ValueError, IndexError):
        await cq.answer("Нет такого подарка", show_alert=True)
        return
    if not (await state.get_data()).get("code"):
        await cq.answer("Сессия сброшена, начни заново", show_alert=True)
        return
    await state.update_data(reward={"gifts": [name]})
    await cq.answer(name)
    await _ask_uses(cq.message.chat.id, state)


@dp.message(Form.promo_ton)
async def pr_ton_msg(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        amount = float((message.text or "").replace(",", ".").strip())
    except ValueError:
        amount = 0
    if not (0 < amount <= 1_000_000):
        await message.answer("Нужно число больше 0, например 5 или 0.5.", reply_markup=_cancel_kb())
        return
    await state.update_data(reward={"ton": round(amount, 2)})
    await _ask_uses(message.chat.id, state)


async def pr_finish(chat_id: int, admin_id: int, state: FSMContext, max_uses: int):
    data = await state.get_data()
    code, reward = data.get("code"), data.get("reward")
    await state.clear()
    if not code or not reward:
        await bot.send_message(chat_id, "Сессия сброшена, начни заново.", reply_markup=promos_menu_kb())
        return
    ok, res = local_db.create_promo(code, reward, max_uses, admin_id)
    if not ok:
        err = "такой код уже существует" if res == "exists" else "некорректный код"
        await bot.send_message(chat_id, f"Не удалось создать промокод: {err}.", reply_markup=promos_menu_kb())
        return
    link = f"https://t.me/{BOT_USERNAME}?start=promo_{code}"
    await bot.send_message(
        chat_id,
        f"<b>Промокод создан</b>\n\nКод: <code>{esc(code)}</code>\nНаграда: {esc(describe_reward(reward))}\n"
        f"Активаций: {max_uses}\n\nСсылка для раздачи:\n{esc(link)}\n\n"
        "Игроки вводят код во вкладке «Промо» в приложении или в боте.",
        parse_mode="HTML",
        reply_markup=promos_menu_kb(),
    )


@dp.callback_query(F.data.startswith("pr_u:"))
async def pr_uses_cb(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    await cq.answer()
    await pr_finish(cq.message.chat.id, cq.from_user.id, state, max(1, int(cq.data.split(":")[1])))


@dp.message(Form.promo_uses)
async def pr_uses_msg(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        n = int((message.text or "").strip())
    except ValueError:
        n = 0
    if not (1 <= n <= 1_000_000):
        await message.answer("Нужно целое число от 1 до 1 000 000.", reply_markup=_cancel_kb())
        return
    await pr_finish(message.chat.id, message.from_user.id, state, n)


@dp.callback_query(F.data == "pr_list")
async def pr_list(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    promos = local_db.list_promos()
    if not promos:
        await cq.message.edit_text("Промокодов пока нет.", reply_markup=promos_menu_kb())
        await cq.answer()
        return
    lines, rows = [], []
    for p in promos[:10]:
        active = p.get("active", True)
        lines.append(f"{p['code']} | {describe_reward(p['reward'])} | {p.get('used', 0)}/{p.get('max_uses', 1)} | "
                     f"{'вкл' if active else 'выкл'}")
        rows.append([
            InlineKeyboardButton(text=f"{'Выкл' if active else 'Вкл'} {p['code']}", callback_data=f"pr_tg:{p['code']}"),
            InlineKeyboardButton(text=f"Удалить {p['code']}", callback_data=f"pr_del:{p['code']}"),
        ])
    rows.append([InlineKeyboardButton(text="Back", callback_data="adm_promos")])
    head = "Промокоды (код | награда | использовано | статус):\n\n"
    try:
        await cq.message.edit_text(head + "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass
    await cq.answer()


@dp.callback_query(F.data.startswith("pr_tg:"))
async def pr_toggle(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    p = local_db.get_promo(cq.data.split(":", 1)[1])
    if p:
        local_db.set_promo_active(p["code"], not p.get("active", True))
    await pr_list(cq)


@dp.callback_query(F.data.startswith("pr_del:"))
async def pr_delete(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    local_db.delete_promo(cq.data.split(":", 1)[1])
    await pr_list(cq)


# ================================================================== GIFT WITHDRAWALS
def app_url(**params) -> str:
    from urllib.parse import urlencode
    params["t"] = int(time.time())
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}{urlencode(params)}"


def restore_kb(wd_id: str, extra_rows: list | None = None) -> InlineKeyboardMarkup:
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton(text="Вернуть подарок в инвентарь",
                                      web_app=WebAppInfo(url=app_url(wd_fail=wd_id)))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_staff(text: str, kb: InlineKeyboardMarkup | None = None):
    for aid in staff_ids():
        try:
            await bot.send_message(aid, text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            log.warning("notify staff %s: %s", aid, e)


async def process_withdraw_request(message: types.Message, arg: str):
    user = message.from_user
    uid = user.id  # id берём из Telegram, а не из ссылки
    parts = arg.split("_")
    slug = ""
    try:
        if len(parts) == 5:  # wd_{uid}_{slug}_{value}_{id}
            slug, value, wd_id = parts[2], float(parts[3].replace("p", ".")), parts[4]
        else:  # старый формат: wd_{uid}_{value}_{short}
            value, wd_id = float(parts[2]), (parts[3] if len(parts) > 3 else arg)
    except (ValueError, IndexError):
        await message.answer("Не удалось прочитать заявку. Создай её ещё раз из приложения.",
                             reply_markup=main_menu_kb(uid))
        return
    gift_name = gift_catalog.SLUG_TO_NAME.get(slug) or "Gift"

    prev = local_db.find_withdraw(wd_id)
    if prev:
        if int(prev.get("uid") or 0) == uid:
            await message.answer(f"Заявка #{prev['id']} уже принята (статус: {prev.get('status')}).",
                                 reply_markup=main_menu_kb(uid))
        else:
            await message.answer("Заявка не найдена.", reply_markup=main_menu_kb(uid))
        return

    if value < MIN_WITHDRAW_TON:
        await message.answer(
            f"Выводить можно только подарки стоимостью от {fmt_num(MIN_WITHDRAW_TON)} TON. "
            "Подарок можно продать в приложении.",
            reply_markup=restore_kb(wd_id),
        )
        return

    if not local_db.wd_active(uid):
        await message.answer(
            f"Вывод закрыт. Чтобы открыть его на {WD_WINDOW_DAYS} дней, пополни баланс одним платежом "
            f"от {WD_UNLOCK_STARS} Stars, потом создай заявку снова.",
            reply_markup=restore_kb(wd_id, [[InlineKeyboardButton(text=f"Пополнить {WD_UNLOCK_STARS} Stars",
                                                                  callback_data=f"pay:{WD_UNLOCK_STARS}")]]),
        )
        return

    w = local_db.add_withdraw({
        "uid": uid,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "item_name": gift_name,
        "item_value": value,
        "item_nft": True,
        "slug": slug,
        "wd_id": wd_id,
        "wd_short": wd_id,
        "status": "pending",
    })
    until = local_db.wd_until(uid)
    await message.answer(
        f"Заявка на вывод #{w['id']} принята.\nПодарок: {gift_name} ({fmt_num(value)} TON).\n"
        "Он остаётся в инвентаре со статусом «Вывод», администратор отправит его в течение 7 дней.\n"
        f"Вывод открыт до {fmt_dt(until)}.",
        reply_markup=main_menu_kb(uid),
    )
    uname = f" @{esc(user.username)}" if user.username else ""
    text = (
        f"<b>Заявка на вывод #{w['id']}</b>\n\n"
        f"Подарок: <b>{esc(gift_name)}</b> ({fmt_num(value)} TON)\n"
        f"Игрок: <a href=\"tg://user?id={uid}\">{esc(user.first_name or 'игрок')}</a>{uname}\n"
        f"ID: <code>{uid}</code>\n"
        f"Окно вывода до: {fmt_dt(until)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправлено", callback_data=f"wd_ok:{w['id']}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"wd_no:{w['id']}"),
    ]])
    await notify_staff(text, kb)


@dp.callback_query(F.data == "wd_info")
async def wd_info(cq: types.CallbackQuery):
    if not await require_subscription(cq):
        return
    until = local_db.wd_until(cq.from_user.id)
    rows = []
    if until > int(time.time() * 1000):
        text = (f"Вывод подарков открыт до {fmt_dt(until)}.\n"
                f"Выводить можно подарки стоимостью от {fmt_num(MIN_WITHDRAW_TON)} TON, бесплатно.")
    else:
        text = (f"Вывод подарков закрыт.\nПополни баланс одним платежом от {WD_UNLOCK_STARS} Stars, "
                f"и {WD_WINDOW_DAYS} дней можно будет бесплатно выводить подарки "
                f"стоимостью от {fmt_num(MIN_WITHDRAW_TON)} TON.")
        rows.append([InlineKeyboardButton(text=f"Пополнить {WD_UNLOCK_STARS} Stars",
                                          callback_data=f"pay:{WD_UNLOCK_STARS}")])
    rows.append([InlineKeyboardButton(text="Back", callback_data="back_menu")])
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@dp.callback_query(F.data == "adm_withdraws")
async def adm_withdraws(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    items = local_db.pending_withdraws()
    if not items:
        await cq.message.edit_text("No withdraw requests.", reply_markup=admin_kb())
        await cq.answer()
        return
    lines, rows = [], []
    for w in items[:10]:
        wid = w.get("id")
        lines.append(f"#{wid} | {w.get('item_name')} {fmt_num(w.get('item_value') or 0)} TON | "
                     f"uid {w.get('uid')} @{w.get('username') or '-'}")
        rows.append([
            InlineKeyboardButton(text=f"Отправлено #{wid}", callback_data=f"wd_ok:{wid}"),
            InlineKeyboardButton(text=f"Отклонить #{wid}", callback_data=f"wd_no:{wid}"),
        ])
    rows.append([InlineKeyboardButton(text="Back", callback_data="admin")])
    await cq.message.edit_text("Заявки на вывод:\n\n" + "\n".join(lines),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


async def _finish_withdraw(cq: types.CallbackQuery, status: str):
    if not is_admin(cq.from_user.id):
        await cq.answer("No access")
        return
    key = cq.data.split(":")[1]
    w = local_db.set_withdraw_status(key, status, cq.from_user.id)
    if not w:
        await cq.answer("Заявка уже обработана или не найдена", show_alert=True)
        return
    uid = int(w.get("uid") or 0)
    ref = w.get("wd_id") or w.get("wd_short") or str(w.get("id"))
    if uid:
        try:
            if status == "sent":
                await bot.send_message(
                    uid,
                    f"Вывод выполнен: {w.get('item_name')} отправлен тебе.\n"
                    "Нажми кнопку, чтобы убрать подарок из инвентаря.",
                    reply_markup=app_kb_url(app_url(wd_ok=ref), "Открыть приложение"),
                )
            else:
                await bot.send_message(
                    uid,
                    f"Заявка на вывод {w.get('item_name')} отклонена. Нажми кнопку, чтобы вернуть подарок в инвентарь.",
                    reply_markup=restore_kb(ref),
                )
        except Exception as e:
            log.warning("wd notify user: %s", e)
    who = f"@{cq.from_user.username}" if cq.from_user.username else str(cq.from_user.id)
    label = "Отправлено" if status == "sent" else "Отклонено"
    msg_text = cq.message.text or ""
    if msg_text.startswith("Заявки на вывод"):
        await cq.answer(label)
        await adm_withdraws(cq)
        return
    try:
        await cq.message.edit_text(f"{cq.message.html_text}\n\n<b>{label}</b> ({esc(who)})", parse_mode="HTML")
    except Exception:
        pass
    await cq.answer(label)


def app_kb_url(url: str, text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


@dp.callback_query(F.data.startswith("wd_ok:"))
async def wd_ok(cq: types.CallbackQuery):
    await _finish_withdraw(cq, "sent")


@dp.callback_query(F.data.startswith("wd_no:"))
async def wd_no(cq: types.CallbackQuery):
    await _finish_withdraw(cq, "rejected")



async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")
    global BOT_USERNAME
    try:
        BOT_USERNAME = (await bot.get_me()).username or BOT_USERNAME
    except Exception as e:
        log.warning("get_me: %s", e)
    if WEBAPP_URL.startswith("https://maxbeten602-coder.github.io/IzbranikGifts/"):
        log.warning("WEBAPP_URL не задан — кнопки «Open app» не будут работать. Укажи ссылку на мини-апп в .env")
    try:
        await bot.delete_webhook(drop_pending_updates=False)  # polling не работает, если у бота висит webhook
    except Exception as e:
        log.warning("delete_webhook: %s", e)
    await start_local_server()
    asyncio.create_task(ban_expiry_loop())
    log.info("Bot starting as @%s (data dir: %s) ...", BOT_USERNAME, DATA_DIR)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
