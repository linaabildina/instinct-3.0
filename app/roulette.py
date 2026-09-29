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
                  "Ты выиграл <b>1 чек</b>. Маленький, но официальный трофей Инстинкта. Чек придёт по почте в течение дня."),
    RoulettePrize("yuan", "1 юань", "🐉", 25000,
                  "Тебе выпал <b>1 юань</b>. Дракон сегодня решил быть щедрым. 🐉"),
    RoulettePrize("zero", "0", "🕳️", 35000,
                  "Поздравляем… с абсолютно ничего. 😂 Судьба посмотрела на тебя и сказала: «Сегодня без подарков»."),
    RoulettePrize("master_dinner", "Ужин с Мастером клана", "🍖", 10000,
                  "<b>Мастер обязан провести с победителем совместный вечер или активность.</b> Мастер не отвертится."),
    RoulettePrize("clan_title", "Клановый титул", "👑", 100,
                  "Ты получаешь <b>уникальный клановый титул</b> от гильдии."),
    RoulettePrize("clan_list_title", "Титул в клан-листе", "📜", 6000,
                  "Ты получаешь <b>специальный титул в клановом списке</b> на определённый срок. Теперь твой ник официально выглядит важнее, чем был пять минут назад."),
    RoulettePrize("vip_person", "VIP-персона клана", "🏅", 5000,
                  "На <b>один день</b> ты становишься VIP-персоной Инстинкта. Особый статус, особое внимание и полное право немного этим похвастаться. 😎"),
    RoulettePrize("secret_gift", "Секретный подарок", "🎁", 3000,
                  "Ты выиграл <b>секретный подарок</b>. Что именно это будет — заранее никто не знает. Подробности объявит Алина или Мастер."),
    RoulettePrize("genius_instinct", "Гений Инстинкта", "🧠", 1800,
                  "На <b>24 часа</b> ты официально становишься Гением Инстинкта. Алина запомнит твой ник и в течение этих 24 часов будет обращаться к тебе соответствующим образом. 😏"),
    RoulettePrize("boss_loot", "Забрать лут с босса КХ 9 этап", "🐉", 500,
                  "Ты получаешь право <b>1 раз забрать лут с босса КХ 9 этап</b>. Условие обязательное: ты должен присутствовать на КХ на всех этапах."),
    RoulettePrize("alina_kiss", "Поцелуй от Алины", "💋", 13500,
                  "Алина обязуется официально поздравить победителя самым пафосным поздравлением, которое только сможет придумать. Победитель получает персональное поздравление от Алины — настолько эпичное и кринжовое, что его захочется сохранить на память. 😂"),
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


def roulette_stats(db_path: str, chat_id: int):
    with _db(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM roulette_spins WHERE chat_id=?",
            (chat_id,),
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT prize_id, prize_title, COUNT(*) AS cnt
               FROM roulette_spins
               WHERE chat_id=?
               GROUP BY prize_id, prize_title
               ORDER BY cnt DESC""",
            (chat_id,),
        ).fetchall()
    return total, rows
