from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    RoulettePrize("empty_gift", "Пустой подарок", "🎁", 1,
                  "Ты получил <b>ПУСТОЙ ПОДАРОК</b>. Внутри ничего нет. Алина уверяет, что это очень редкий предмет. 😂"),
    RoulettePrize("golden_air", "Золотой воздух", "💨", 1,
                  "Ты получил <b>ЗОЛОТОЙ ВОЗДУХ</b>. Дышать можно как обычно. Но он золотой. ✨"),
    RoulettePrize("good_person_certificate", "Справка «Я сегодня молодец»", "📜", 1,
                  "Официальная справка от Алины: <b>ты сегодня молодец</b>. Оснований нет. Но Алина решила. 😌"),
    RoulettePrize("alina_question_coupon", "Купон на вопрос Алине", "🎟️", 1,
                  "Ты получил купон на <b>один бесплатный вопрос Алине</b>. Да, спрашивать можно было и без него. Но теперь это официально. 😂"),
    RoulettePrize("legendary_potato", "Легендарная картошка Инстинкта", "🥔", 1,
                  "Легендарный артефакт неизвестного назначения. Никто не знает, зачем она нужна. Но Алина говорит, что это очень важно."),
    RoulettePrize("lucky_sock", "Носок абсолютной удачи", "🧦", 1,
                  "Носок приносит абсолютную удачу. Второго носка не существует. В этом и заключается его сила."),
    RoulettePrize("useless_power_crystal", "Кристалл бесполезной мощи", "💎", 1,
                  "Даёт <b>+999 к понтам</b> и <b>+0 к характеристикам</b>. Редкий, красивый и совершенно бесполезный."),
    RoulettePrize("busy_wand", "Палочка «Сделай вид, что я занят»", "🪄", 1,
                  "Позволяет убедительно выглядеть занятым, ничего не делая. Алина официально одобряет."),
    RoulettePrize("audacity_license", "Лицензия на наглость", "🪪", 1,
                  "Разрешает быть наглым целых <b>24 часа</b>. Алина постарается не осуждать."),
    RoulettePrize("alina_eye", "Око Алины", "👀", 1,
                  "Алина теперь знает, что ты опять ничего не делаешь. <b>Скрыться невозможно.</b> 😈"),
    RoulettePrize("dragon_scale", "Чешуя Дракона", "🐉", 1,
                  "Защищает от критики, здравого смысла и некоторых сомнительных решений. Эффект научно не подтверждён."),
    RoulettePrize("favorite_crown", "Корона главного любимчика", "👑", 1,
                  "На <b>24 часа</b> ты официально главный любимчик Алины. Остальным разрешается завидовать."),
    RoulettePrize("alina_brain", "Мозг Алины на прокат", "🧠", 1,
                  "Легендарный предмет! Алина временно передала тебе <b>0,5% своего мозга</b> на 24 часа. Вернуть желательно. 😂"),
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
    conn.execute("""CREATE TABLE IF NOT EXISTS roulette_claims (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        spin_date TEXT NOT NULL,
        prize_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, user_id, spin_date)
    )""")
    conn.commit()
    return conn


def cooldown_remaining(db_path: str, chat_id: int, user_id: int) -> int:
    """Seconds remaining until 24 hours have passed since the last spin."""
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT created_at FROM roulette_spins WHERE chat_id=? AND user_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id, user_id),
        ).fetchone()
    if not row or not row[0]:
        return 0
    try:
        last = datetime.fromisoformat(row[0])
        remaining = (last + timedelta(hours=24) - datetime.now(TZ)).total_seconds()
        return max(0, int(remaining))
    except Exception:
        return 0


def spun_today(db_path: str, chat_id: int, user_id: int) -> bool:
    return cooldown_remaining(db_path, chat_id, user_id) > 0


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
        if payload.get("u") != user_id:
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


def claim_spin(db_path: str, chat_id: int, user_id: int, prize: RoulettePrize) -> bool:
    """Atomically marks the user's current spin prize as claimed."""
    day = today_key()
    with _db(db_path) as conn:
        spin = conn.execute(
            "SELECT 1 FROM roulette_spins WHERE chat_id=? AND user_id=? AND spin_date=? AND prize_id=?",
            (chat_id, user_id, day, prize.id),
        ).fetchone()
        if not spin:
            return False
        try:
            conn.execute(
                """INSERT INTO roulette_claims(chat_id,user_id,spin_date,prize_id,claimed_at)
                   VALUES(?,?,?,?,?)""",
                (chat_id, user_id, day, prize.id, datetime.now(TZ).isoformat()),
            )
            return True
        except sqlite3.IntegrityError:
            return False
