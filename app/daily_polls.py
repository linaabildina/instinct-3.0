"""Daily surprise polls for Alina: one original, humorous poll per day."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Almaty")
TOPIC_ID = 2


class DailyPollManager:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alina_daily_polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    poll_id TEXT UNIQUE,
                    question TEXT NOT NULL,
                    theme TEXT NOT NULL DEFAULT '',
                    options_json TEXT NOT NULL,
                    poll_date TEXT NOT NULL,
                    thread_id INTEGER,
                    message_id INTEGER,
                    closed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    commentary_status TEXT NOT NULL DEFAULT 'pending',
                    commentary TEXT
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(alina_daily_polls)").fetchall()}
            if "message_id" not in columns:
                conn.execute("ALTER TABLE alina_daily_polls ADD COLUMN message_id INTEGER")
            if "closed" not in columns:
                conn.execute("ALTER TABLE alina_daily_polls ADD COLUMN closed INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alina_daily_polls_chat_date "
                "ON alina_daily_polls(chat_id, poll_date)"
            )

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def was_sent_today(self, chat_id: int, date_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alina_daily_polls WHERE chat_id=? AND poll_date=? LIMIT 1",
                (chat_id, date_key),
            ).fetchone()
        return row is not None

    def recent_history(self, chat_id: int, limit: int = 80) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT question, theme, options_json, poll_date FROM alina_daily_polls "
                "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        history = []
        for row in rows:
            try:
                options = json.loads(row["options_json"])
            except (TypeError, json.JSONDecodeError):
                options = []
            history.append({
                "question": row["question"],
                "theme": row["theme"],
                "options": options,
                "date": row["poll_date"],
            })
        return history

    def save_poll(self, chat_id: int, poll_id: str, question: str, theme: str,
                  options: list[str], poll_date: str, thread_id: int | None, message_id: int):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alina_daily_polls "
                "(chat_id, poll_id, question, theme, options_json, poll_date, thread_id, message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, str(poll_id), question, theme, json.dumps(options, ensure_ascii=False),
                 poll_date, thread_id, message_id, datetime.now(TZ).isoformat()),
            )

    def expired_polls(self, age_seconds: int = 86400) -> list[dict]:
        cutoff = (datetime.now(TZ) - timedelta(seconds=age_seconds)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, poll_id, message_id FROM alina_daily_polls "
                "WHERE closed=0 AND message_id IS NOT NULL AND created_at<=?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_closed(self, poll_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE alina_daily_polls SET closed=1 WHERE poll_id=?",
                (str(poll_id),),
            )

    def get_poll(self, poll_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alina_daily_polls WHERE poll_id=?",
                (str(poll_id),),
            ).fetchone()
        return dict(row) if row else None

    def claim_commentary(self, poll_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE alina_daily_polls SET commentary_status='processing' "
                "WHERE poll_id=? AND commentary_status='pending'",
                (str(poll_id),),
            )
            return cur.rowcount == 1

    def finish_commentary(self, poll_id: str, commentary: str | None):
        with self._connect() as conn:
            conn.execute(
                "UPDATE alina_daily_polls SET commentary_status=?, commentary=? WHERE poll_id=?",
                ("done" if commentary else "pending", commentary, str(poll_id)),
            )


def _extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", raw, flags=re.I)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI did not return a JSON object")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI response is not an object")
    return data


async def _generate_poll(ai, history: list[dict]) -> dict:
    recent = json.dumps(history, ensure_ascii=False)
    prompt = (
        "Придумай ОДНО совершенно новое внезапное смешное голосование для общего дружеского "
        "Telegram-чата гильдии. НЕ про игру, Perfect World, ГВГ, экипировку или игровые механики. "
        "Темы: бытовой абсурд, отношения, странные гипотезы, неловкие ситуации, социальные эксперименты, "
        "чёрный (но безопасный) юмор, нелепые законы, привычки людей, неожиданные дилеммы. "
        "Это должно быть реально смешно и провоцировать обсуждение, а не звучать как корпоративная анкета. "
        "Каждый раз выбирай новую тему и новый комедийный механизм; не перефразируй старые идеи. "
        "Не делай травлю конкретных участников, не унижай защищённые группы, не используй опасные задания. "
        "Вопрос короткий и цепляющий, варианты ответа остроумные и различаются по смыслу. "
        "Верни ТОЛЬКО JSON вида: {\"question\":\"...\",\"theme\":\"короткая категория\","
        "\"options\":[\"...\",\"...\",\"...\",\"...\"]}. "
        "Вопрос максимум 250 символов; от 4 до 6 вариантов, каждый максимум 90 символов. "
        "Не включай markdown и пояснения.\n"
        f"История предыдущих опросов — не повторяй вопросы, сюжеты, темы и шутки: {recent}"
    )
    raw = await ai._generate([
        {"role": "system", "content": getattr(ai, "system_prompt", "")},
        {"role": "user", "content": prompt},
    ])
    data = _extract_json(raw)
    question = str(data.get("question", "")).strip()
    theme = str(data.get("theme", "абсурд")).strip()[:80]
    options = data.get("options")
    if not question or len(question) > 300:
        raise ValueError("Invalid poll question")
    if not isinstance(options, list):
        raise ValueError("Poll options are missing")
    options = [str(item).strip()[:100] for item in options if str(item).strip()]
    if not 2 <= len(options) <= 10 or len(set(options)) != len(options):
        raise ValueError("Poll must contain 2-10 distinct options")
    return {"question": question, "theme": theme, "options": options}


async def run_daily_polls(bot, ai, manager: DailyPollManager, chat_id: int,
                          topic_id: int = TOPIC_ID):
    """Schedule one surprise poll on a random local-time moment each calendar day."""
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date().isoformat()
            if manager.was_sent_today(chat_id, today):
                next_day = now.date() + timedelta(days=1)
                start = datetime.combine(next_day, datetime.min.time(), TZ)
            else:
                start = now + timedelta(minutes=2)
                end_of_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), TZ)
                if start >= end_of_day - timedelta(minutes=1):
                    start = end_of_day

            if start.date() == now.date():
                end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), TZ)
                available_minutes = max(1, int((end - start).total_seconds() // 60))
                target = start + timedelta(minutes=random.randint(0, available_minutes - 1))
            else:
                # A fresh random time between 00:05 and 23:55 for the next day.
                target = start + timedelta(minutes=random.randint(5, 1435))

            await asyncio.sleep(max(1, (target - datetime.now(TZ)).total_seconds()))
            now = datetime.now(TZ)
            poll_date = now.date().isoformat()
            if manager.was_sent_today(chat_id, poll_date):
                continue

            history = manager.recent_history(chat_id)
            data = await _generate_poll(ai, history)
            message = await bot.send_poll(
                chat_id=chat_id,
                question=data["question"],
                options=data["options"],
                is_anonymous=True,
                allows_multiple_answers=False,
                message_thread_id=topic_id,
            )
            manager.save_poll(
                chat_id, message.poll.id, data["question"], data["theme"],
                data["options"], poll_date, topic_id, message.message_id,
            )
            logging.info("Alina daily poll posted: theme=%s poll_id=%s", data["theme"], message.poll.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Alina daily poll failed")
            await asyncio.sleep(10 * 60)


async def handle_poll_update(bot, ai, manager: DailyPollManager, poll):
    """Publish a bespoke humorous verdict when one of Alina's polls closes."""
    if not getattr(poll, "is_closed", False):
        return
    record = manager.get_poll(poll.id)
    if not record or not manager.claim_commentary(poll.id):
        return

    try:
        counts = []
        for option in poll.options:
            counts.append({
                "text": option.text,
                "votes": int(option.voter_count or 0),
            })
        total = sum(item["votes"] for item in counts)
        results = sorted(counts, key=lambda item: item["votes"], reverse=True)
        prompt = (
            "Ты Алина — дерзкая, смешная участница дружеского чата. Голосование завершилось. "
            "Напиши живой авторский комментарий на русском на 2-5 предложений: подведи итог, "
            "смешно интерпретируй именно этот результат, подколы уместны, но без оскорблений. "
            "Не пиши сухой отчёт и не используй шаблонные фразы вроде «народ сделал свой выбор». "
            "Если ничья — преврати её в абсурдный конфликт; если победитель очевиден — вынеси неожиданный вердикт. "
            "Не выдумывай имена участников: опрос анонимный. Верни только текст комментария.\n"
            f"Вопрос: {record['question']}\n"
            f"Варианты и голоса: {json.dumps(counts, ensure_ascii=False)}\n"
            f"Всего голосов: {total}. Победившие варианты: "
            f"{json.dumps([x for x in results if results and x['votes'] == results[0]['votes']], ensure_ascii=False)}"
        )
        commentary = (await ai._generate([
            {"role": "system", "content": getattr(ai, "system_prompt", "")},
            {"role": "user", "content": prompt},
        ])).strip()
        if not commentary or commentary.upper() == "NO_REPLY":
            commentary = (
                "Вердикт вынесен. Я не буду делать далеко идущие выводы о человечестве по одному голосованию… "
                "пока что. 😂"
            )
        await bot.send_message(
            chat_id=record["chat_id"],
            text=commentary,
            message_thread_id=record["thread_id"],
        )
        manager.finish_commentary(poll.id, commentary)
    except asyncio.CancelledError:
        manager.finish_commentary(poll.id, None)
        raise
    except Exception:
        manager.finish_commentary(poll.id, None)
        logging.exception("Alina poll commentary failed for poll %s", poll.id)


async def close_expired_polls_forever(bot, manager: DailyPollManager):
    """Close polls after 24 hours, including after bot restarts."""
    while True:
        try:
            for record in manager.expired_polls():
                try:
                    await bot.stop_poll(
                        chat_id=record["chat_id"],
                        message_id=record["message_id"],
                    )
                    manager.mark_closed(record["poll_id"])
                except Exception:
                    logging.exception("Could not close expired Alina poll %s", record["poll_id"])
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Expired poll closing loop failed")
            await asyncio.sleep(60)
