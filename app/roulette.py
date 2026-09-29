from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Almaty")


@dataclass(frozen=True)
class RoulettePrize:
    id: str
    title: str
    emoji: str
    weight: int
    description: str


PRIZES = (
    RoulettePrize("check", "1 чек", "💸", 100,
                  "Тебе достался <b>1 чек</b>. Всё. Не спрашивай, откуда он взялся. Главное — чек есть. 😎"),
    RoulettePrize("yuan", "1 юань", "🐉", 25000,
                  "Ты выиграл <b>1 юань</b>. Великое состояние начинается с малого. 🤑"),
    RoulettePrize("zero", "0", "🕳️", 35000,
                  "Сегодня судьба решила, что ты достаточно богат духовно. Твой приз — <b>0</b>. 😂"),
    RoulettePrize("master_dinner", "Ужин с Мастером клана", "🍖", 10000,
                  "<b>Мастер обязан провести с победителем вечер или совместную активность.</b> Победитель выбирает формат: ужин, прогулка, совместная игра или другой вариант по договорённости. Мастер не отвертится. 😈"),
    RoulettePrize("clan_title", "Клановый титул", "👑", 100,
                  "Победителю будет выдан <b>уникальный клановый титул</b>. Алина объявит его название и сообщит об этом всему клану. Титул действует до тех пор, пока Мастер не решит заменить его на что-нибудь ещё более легендарное. 👑"),
    RoulettePrize("master_10m", "Мастер на 10 минут", "👑", 7000,
                  "В течение <b>10 минут</b> победитель получает право дать Мастеру клана одно абсурдное, но выполнимое поручение. Мастер обязан выполнить его, если оно не нарушает правила клана. 😈"),
    RoulettePrize("clan_list_title", "Титул в клан-листе", "📜", 6000,
                  "Победитель получает <b>особый титул в клан-листе</b> на определённый срок. Алина сама объявит его название всему клану."),
    RoulettePrize("vip_person", "VIP-персона клана", "🏅", 5000,
                  "На <b>один день</b> победитель официально становится VIP-персоной Инстинкта. Сегодня ему разрешено напоминать всем, насколько он важная персона. 😎"),
    RoulettePrize("clan_voice", "Голос клана", "📢", 4000,
                  "Алина объявляет победителя <b>«Легендой дня»</b> в общем чате и официально сообщает клану о его величии. 📢"),
    RoulettePrize("secret_gift", "Секретный подарок", "🎁", 3000,
                  "Победителю положен <b>секретный подарок</b>. Что именно — заранее неизвестно. Детали будут выданы отдельно. 👀"),
    RoulettePrize("exile_right", "Право на изгнание", "💀", 3000,
                  "Победитель выбирает <b>одного игрока</b>, а Алина выдаёт выбранному игроку шуточный титул на сутки. Изгнание, конечно, только словесное. 😂"),
    RoulettePrize("genius_instinct", "Гений Инстинкта", "🧠", 1800,
                  "На <b>24 часа</b> победитель официально объявляется самым умным человеком клана. Даже если это очевидная ошибка системы. Возражения не принимаются. 🧠👑"),
)

PRIZES_BY_ID = {p.id: p for p in PRIZES}
TOTAL_WEIGHT = sum(p.weight for p in PRIZES)


def today_key() -> str:
    return datetime.now(TZ).date().isoformat()


def _db(db_path: str):
    conn = sqlite3.connect(Path(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS roulette_spins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        display_name TEXT,
        spin_date TEXT NOT NULL,
        prize_id TEXT NOT NULL,
        prize_title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(chat_id, user_id, spin_date)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS roulette_pending (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        spin_date TEXT NOT NULL,
        token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, user_id, spin_date)
    )""")
    conn.commit()
    return conn


def spun_today(db_path: str, chat_id: int, user_id: int) -> bool:
    day = today_key()
    with _db(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM roulette_spins WHERE chat_id=? AND user_id=? AND spin_date=?",
            (chat_id, user_id, day),
        ).fetchone() is not None


def get_pending(db_path: str, chat_id: int, user_id: int):
    day = today_key()
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT token FROM roulette_pending WHERE chat_id=? AND user_id=? AND spin_date=?",
            (chat_id, user_id, day),
        ).fetchone()
    return row[0] if row else None


def save_pending(db_path: str, chat_id: int, user_id: int, token: str):
    day = today_key()
    with _db(db_path) as conn:
        conn.execute(
            """INSERT INTO roulette_pending(chat_id,user_id,spin_date,token,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,user_id,spin_date) DO UPDATE SET
               token=excluded.token, created_at=excluded.created_at""",
            (chat_id, user_id, day, token, datetime.now(TZ).isoformat()),
        )


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def choose_prize_id() -> str:
    point = secrets.randbelow(TOTAL_WEIGHT)
    cursor = 0
    for prize in PRIZES:
        cursor += prize.weight
        if point < cursor:
            return prize.id
    return PRIZES[-1].id


def make_token(bot_token: str, user_id: int, prize_id: str) -> str:
    payload = json.dumps(
        {"u": user_id, "d": today_key(), "p": prize_id},
        ensure_ascii=False, separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(encoded, bot_token)}"


def verify_token(bot_token: str, token: str, user_id: int):
    try:
        encoded, signature = token.split(".", 1)
        expected = _sign(encoded, bot_token)
        if not hmac.compare_digest(signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode((encoded + padding).encode()).decode())
        if payload.get("u") != user_id or payload.get("d") != today_key():
            return None
        prize = PRIZES_BY_ID.get(payload.get("p"))
        return prize
    except Exception:
        return None


def save_spin(db_path: str, chat_id: int, user_id: int, username: str | None,
              display_name: str | None, prize: RoulettePrize) -> bool:
    day = today_key()
    with _db(db_path) as conn:
        try:
            conn.execute(
                """INSERT INTO roulette_spins(
                    chat_id,user_id,username,display_name,spin_date,prize_id,prize_title,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (chat_id, user_id, username, display_name, day, prize.id, prize.title, datetime.now(TZ).isoformat()),
            )
            conn.execute(
                "DELETE FROM roulette_pending WHERE chat_id=? AND user_id=? AND spin_date=?",
                (chat_id, user_id, day),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def public_prizes():
    return [{"id": p.id, "title": p.title, "emoji": p.emoji} for p in PRIZES]
