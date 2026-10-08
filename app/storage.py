from pathlib import Path
import sqlite3
from datetime import datetime, timezone


class Storage:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS user_memory (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT,
                last_seen TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (chat_id, user_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                memory TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                fact TEXT NOT NULL,
                people TEXT,
                source_user_id INTEGER,
                source_username TEXT,
                confidence REAL NOT NULL DEFAULT 0.8,
                created_at TEXT NOT NULL,
                last_confirmed_at TEXT NOT NULL,
                UNIQUE(chat_id, category, fact)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_recruit_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                player_nickname TEXT NOT NULL,
                player_class TEXT,
                level TEXT,
                accepted_by_user_id INTEGER,
                accepted_by_username TEXT,
                accepted_by_display_name TEXT,
                created_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                correction TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS recruits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                game_nickname TEXT NOT NULL,
                level TEXT,
                class_name TEXT,
                teamspeak TEXT,
                telegram TEXT,
                added_by_user_id INTEGER,
                added_by_username TEXT,
                added_by_display_name TEXT,
                created_at TEXT NOT NULL
            )""")
            # Единая миграция старых таблиц recruits.
            # В старых версиях встречались обязательные player_class / accepted_at,
            # из-за чего новая INSERT-запись падала с NOT NULL constraint.
            recruit_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(recruits)").fetchall()
            }
            legacy_columns = {"player_class", "accepted_at"} & recruit_columns
            if legacy_columns:
                conn.execute("ALTER TABLE recruits RENAME TO recruits_legacy")
                conn.execute("""CREATE TABLE recruits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    game_nickname TEXT NOT NULL,
                    level TEXT,
                    class_name TEXT,
                    teamspeak TEXT,
                    telegram TEXT,
                    added_by_user_id INTEGER,
                    added_by_username TEXT,
                    added_by_display_name TEXT,
                    created_at TEXT NOT NULL
                )""")
                legacy_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(recruits_legacy)").fetchall()
                }

                def _expr(column, fallback="NULL"):
                    return column if column in legacy_cols else fallback

                created_expr = (
                    "COALESCE(NULLIF(created_at, ''), accepted_at, CURRENT_TIMESTAMP)"
                    if "created_at" in legacy_cols and "accepted_at" in legacy_cols
                    else _expr("created_at", "CURRENT_TIMESTAMP")
                )
                class_expr = (
                    "COALESCE(NULLIF(class_name, ''), player_class)"
                    if "class_name" in legacy_cols and "player_class" in legacy_cols
                    else _expr("class_name", _expr("player_class", "''"))
                )

                conn.execute(
                    f"""INSERT INTO recruits(
                        id, chat_id, game_nickname, level, class_name, teamspeak, telegram,
                        added_by_user_id, added_by_username, added_by_display_name, created_at
                    )
                    SELECT id, chat_id, game_nickname, {_expr("level", "''")},
                           {class_expr}, {_expr("teamspeak", "''")}, {_expr("telegram", "''")},
                           {_expr("added_by_user_id")}, {_expr("added_by_username")},
                           {_expr("added_by_display_name")}, {created_expr}
                    FROM recruits_legacy"""
                )
                conn.execute("DROP TABLE recruits_legacy")

            # На случай промежуточной старой схемы добавляем отсутствующие поля.
            recruit_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(recruits)").fetchall()
            }
            for column, definition in {
                "class_name": "TEXT",
                "teamspeak": "TEXT",
                "telegram": "TEXT",
                "added_by_user_id": "INTEGER",
                "added_by_username": "TEXT",
                "added_by_display_name": "TEXT",
                "created_at": "TEXT",
            }.items():
                if column not in recruit_columns:
                    if column == "created_at":
                        conn.execute(
                            "ALTER TABLE recruits ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
                        )
                    else:
                        conn.execute(f"ALTER TABLE recruits ADD COLUMN {column} {definition}")

            recruit_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(recruits)").fetchall()
            }
            if "player_class" in recruit_columns and "class_name" in recruit_columns:
                conn.execute(
                    "UPDATE recruits SET class_name = player_class "
                    "WHERE COALESCE(class_name, '') = '' AND player_class IS NOT NULL"
                )
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_join_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT,
                joined_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS clan_join_daily_checks (
                chat_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, check_date)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS newbie_drafts (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                game_nickname TEXT NOT NULL DEFAULT '',
                level TEXT NOT NULL DEFAULT '',
                class_name TEXT NOT NULL DEFAULT '',
                teamspeak TEXT NOT NULL DEFAULT '',
                telegram TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS newbie_draft_messages (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                questionnaire_message_id INTEGER,
                confirmation_message_id INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS newbie_message_log (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id, message_id)
            )""")


    def _conn(self):
        return sqlite3.connect(self.db_path)

    def add(self, chat_id: int, user_id: int | None, username: str | None, role: str, content: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages(chat_id,user_id,username,role,content,created_at) VALUES(?,?,?,?,?,?)",
                (chat_id, user_id, username, role, content, datetime.now(timezone.utc).isoformat()),
            )

    def recent(self, chat_id: int, limit: int = 20):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def is_chat_enabled(self, chat_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM chat_settings WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO chat_settings(chat_id, enabled, updated_at) VALUES(?,?,?)",
                    (chat_id, 1, datetime.now(timezone.utc).isoformat()),
                )
                return True
            return bool(row[0])

    def set_chat_enabled(self, chat_id: int, enabled: bool):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chat_settings(chat_id, enabled, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       enabled=excluded.enabled,
                       updated_at=excluded.updated_at""",
                (chat_id, 1 if enabled else 0, datetime.now(timezone.utc).isoformat()),
            )


    def remember_user(self, chat_id: int, user_id: int | None, username: str | None, display_name: str | None, message: str | None = None):
        if user_id is None:
            return
        with self._conn() as conn:
            row = conn.execute(
                "SELECT messages FROM user_memory WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            history = [x for x in (row[0].split("\n") if row and row[0] else []) if x]
            if message:
                history.append(message.strip()[:500])
                history = history[-10:]
            conn.execute(
                """INSERT INTO user_memory(chat_id,user_id,username,display_name,last_seen,messages)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(chat_id,user_id) DO UPDATE SET
                       username=excluded.username,
                       display_name=excluded.display_name,
                       last_seen=excluded.last_seen,
                       messages=excluded.messages""",
                (chat_id, user_id, username, display_name, datetime.now(timezone.utc).isoformat(), "\n".join(history)),
            )

    def user_last_seen(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            row = conn.execute("SELECT last_seen FROM user_memory WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return row[0] if row else None


    def add_clan_memory(self, chat_id: int, memory: str):
        memory = memory.strip()
        if not memory:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO clan_memory(chat_id,memory,created_at) VALUES(?,?,?)",
                (chat_id, memory[:1000], datetime.now(timezone.utc).isoformat()),
            )

    def clan_memories(self, chat_id: int, limit: int = 30):
        with self._conn() as conn:
            return conn.execute(
                "SELECT id, memory, created_at FROM clan_memory WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()

    def add_clan_memory_fact(
        self,
        chat_id: int,
        category: str,
        fact: str,
        people: str | None = None,
        source_user_id: int | None = None,
        source_username: str | None = None,
        confidence: float = 0.8,
    ):
        fact = " ".join((fact or "").split()).strip()
        category = " ".join((category or "").split()).strip().lower()
        people = " ".join((people or "").split()).strip() or None
        if not fact or category not in {"players", "achievement", "event", "meme", "trolling", "important"}:
            return False
        confidence = max(0.0, min(1.0, float(confidence or 0.8)))
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """SELECT id FROM clan_memory_facts
                   WHERE chat_id=? AND category=? AND lower(fact)=lower(?)""",
                (chat_id, category, fact),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE clan_memory_facts
                       SET people=?, source_user_id=?, source_username=?,
                           confidence=?, last_confirmed_at=?
                       WHERE id=?""",
                    (people, source_user_id, source_username, confidence, now, row[0]),
                )
            else:
                conn.execute(
                    """INSERT INTO clan_memory_facts(
                       chat_id, category, fact, people, source_user_id,
                       source_username, confidence, created_at, last_confirmed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (chat_id, category, fact, people, source_user_id, source_username,
                     confidence, now, now),
                )
        return True

    def clan_memory_facts(self, chat_id: int, limit: int = 80):
        with self._conn() as conn:
            return conn.execute(
                """SELECT id, category, fact, people, confidence, created_at, last_confirmed_at
                   FROM clan_memory_facts
                   WHERE chat_id=?
                   ORDER BY last_confirmed_at DESC, id DESC
                   LIMIT ?""",
                (chat_id, limit),
            ).fetchall()

    def user_memories(self, chat_id: int, limit: int = 30):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT username, display_name, messages, last_seen FROM user_memory WHERE chat_id=? ORDER BY last_seen DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return rows

    def add_knowledge_correction(self, chat_id: int, correction: str):
        correction = correction.strip()
        if not correction:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO knowledge_corrections(chat_id,correction,created_at) VALUES(?,?,?)",
                (chat_id, correction[:1500], datetime.now(timezone.utc).isoformat()),
            )

    def knowledge_corrections(self, chat_id: int, limit: int = 30):
        with self._conn() as conn:
            return conn.execute(
                "SELECT id, correction, created_at FROM knowledge_corrections WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()


    def add_recruit(self, chat_id: int, game_nickname: str, level: str, class_name: str,
                    teamspeak: str, telegram: str, added_by_user_id: int | None,
                    added_by_username: str | None, added_by_display_name: str | None):
        """Сохраняет принятого новичка локально. Повторное принятие обновляет запись."""
        game_nickname = game_nickname.strip()
        if not game_nickname:
            return False

        with self._conn() as conn:
            exists = conn.execute(
                "SELECT id FROM recruits WHERE chat_id=? AND lower(game_nickname)=lower(?) LIMIT 1",
                (chat_id, game_nickname),
            ).fetchone()

            values = (
                level.strip(),
                class_name.strip(),
                teamspeak.strip(),
                telegram.strip(),
                added_by_user_id,
                added_by_username.lower().lstrip("@").strip() if added_by_username else None,
                added_by_display_name,
                datetime.now(timezone.utc).isoformat(),
            )

            if exists:
                conn.execute(
                    """UPDATE recruits SET
                       level=?, class_name=?, teamspeak=?, telegram=?,
                       added_by_user_id=?, added_by_username=?,
                       added_by_display_name=?, created_at=?
                       WHERE id=?""",
                    (*values, exists[0]),
                )
            else:
                recruit_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(recruits)").fetchall()
                }
                if "player_class" in recruit_columns:
                    # Старые версии БД требуют player_class как NOT NULL.
                    conn.execute(
                        """INSERT INTO recruits(
                            chat_id, game_nickname, level, class_name, player_class,
                            teamspeak, telegram, added_by_user_id, added_by_username,
                            added_by_display_name, created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            chat_id, game_nickname, values[0], values[1], values[1],
                            values[2], values[3], values[4], values[5], values[6], values[7],
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO recruits(
                            chat_id, game_nickname, level, class_name, teamspeak, telegram,
                            added_by_user_id, added_by_username, added_by_display_name, created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            chat_id, game_nickname, *values,
                        ),
                    )

            count = conn.execute(
                "SELECT COUNT(*) FROM recruits WHERE chat_id=?",
                (chat_id,),
            ).fetchone()[0]

        return True

    def all_recruits(self):
        """Возвращает всех принятых новичков для локального раздела EXE."""
        with self._conn() as conn:
            return conn.execute(
                """SELECT game_nickname, level, class_name, teamspeak, telegram,
                          added_by_username, added_by_display_name, created_at
                   FROM recruits
                   ORDER BY id DESC"""
            ).fetchall()

    def recruits_by_adder(self, chat_id: int, username: str):
        username = username.lower().lstrip("@").strip()
        with self._conn() as conn:
            return conn.execute(
                """SELECT game_nickname, level, class_name, teamspeak, telegram, created_at
                   FROM recruits
                   WHERE chat_id=? AND lower(COALESCE(added_by_username,''))=?
                   ORDER BY id ASC""",
                (chat_id, username),
            ).fetchall()

    def recruits_by_adder_user_id(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            return conn.execute(
                """SELECT game_nickname, level, class_name, teamspeak, telegram, created_at
                   FROM recruits
                   WHERE chat_id=? AND added_by_user_id=?
                   ORDER BY id ASC""",
                (chat_id, user_id),
            ).fetchall()

    def recruit_analytics_all(self, chat_id: int):
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM recruits WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            by_adder = conn.execute(
                """SELECT added_by_username, added_by_display_name, COUNT(*) AS total
                   FROM recruits
                   WHERE chat_id=?
                   GROUP BY added_by_user_id, added_by_username, added_by_display_name
                   ORDER BY total DESC, COALESCE(added_by_username, added_by_display_name, '') ASC""",
                (chat_id,),
            ).fetchall()
        return total, by_adder

    def recruit_analytics_user(self, chat_id: int, username: str):
        username = username.lower().lstrip("@").strip()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT game_nickname, level, class_name, teamspeak, telegram, created_at
                   FROM recruits
                   WHERE chat_id=? AND lower(COALESCE(added_by_username,''))=?
                   ORDER BY id ASC""",
                (chat_id, username),
            ).fetchall()
        return rows

    def recruit_by_nickname(self, chat_id: int, game_nickname: str):
        with self._conn() as conn:
            return conn.execute(
                """SELECT game_nickname, level, class_name, teamspeak, telegram,
                          added_by_username, added_by_display_name, created_at
                   FROM recruits
                   WHERE chat_id=? AND lower(game_nickname)=lower(?)
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, game_nickname.strip()),
            ).fetchone()


    def record_clan_join(self, chat_id: int, user_id: int, username: str | None, display_name: str | None):
        if user_id is None:
            return
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO clan_join_events(chat_id,user_id,username,display_name,joined_at)
                   VALUES(?,?,?,?,?)""",
                (chat_id, user_id, username, display_name, datetime.now(timezone.utc).isoformat()),
            )

    def clan_joins_since(self, chat_id: int, since: datetime):
        with self._conn() as conn:
            return conn.execute(
                """SELECT user_id, username, display_name, joined_at
                   FROM clan_join_events
                   WHERE chat_id=? AND joined_at>=?
                   ORDER BY joined_at ASC""",
                (chat_id, since.astimezone(timezone.utc).isoformat()),
            ).fetchall()

    def daily_join_check_claim(self, chat_id: int, check_date: str) -> bool:
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO clan_join_daily_checks(chat_id,check_date,checked_at) VALUES(?,?,?)",
                    (chat_id, check_date, datetime.now(timezone.utc).isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def save_newbie_draft(self, chat_id: int, user_id: int, data: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO newbie_drafts(
                    chat_id,user_id,game_nickname,level,class_name,teamspeak,telegram,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    game_nickname=excluded.game_nickname,
                    level=excluded.level,
                    class_name=excluded.class_name,
                    teamspeak=excluded.teamspeak,
                    telegram=excluded.telegram,
                    updated_at=excluded.updated_at""",
                (
                    chat_id, user_id,
                    data.get("game_nickname",""),
                    data.get("level",""),
                    data.get("class_name",""),
                    data.get("teamspeak",""),
                    data.get("telegram",""),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_newbie_draft(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            row = conn.execute(
                """SELECT game_nickname,level,class_name,teamspeak,telegram
                   FROM newbie_drafts WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()
        if not row:
            return None
        keys = ("game_nickname","level","class_name","teamspeak","telegram")
        return dict(zip(keys, row))

    def delete_newbie_draft(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM newbie_drafts WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )

    def save_newbie_message_ids(self, chat_id: int, user_id: int, questionnaire_message_id: int | None = None,
                                confirmation_message_id: int | None = None):
        with self._conn() as conn:
            current = conn.execute(
                "SELECT questionnaire_message_id, confirmation_message_id FROM newbie_draft_messages WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            qid = questionnaire_message_id if questionnaire_message_id is not None else (current[0] if current else None)
            cid = confirmation_message_id if confirmation_message_id is not None else (current[1] if current else None)
            conn.execute(
                """INSERT INTO newbie_draft_messages(
                    chat_id,user_id,questionnaire_message_id,confirmation_message_id,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    questionnaire_message_id=excluded.questionnaire_message_id,
                    confirmation_message_id=excluded.confirmation_message_id,
                    updated_at=excluded.updated_at""",
                (chat_id, user_id, qid, cid, datetime.now(timezone.utc).isoformat()),
            )

    def get_newbie_message_ids(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            return conn.execute(
                """SELECT questionnaire_message_id, confirmation_message_id
                   FROM newbie_draft_messages WHERE chat_id=? AND user_id=?""",
                (chat_id, user_id),
            ).fetchone()

    def delete_newbie_message_ids(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM newbie_draft_messages WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )

    def add_newbie_message(self, chat_id: int, user_id: int, message_id: int):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO newbie_message_log(chat_id,user_id,message_id,created_at)
                   VALUES(?,?,?,?)""",
                (chat_id, user_id, message_id, datetime.now(timezone.utc).isoformat()),
            )

    def get_newbie_messages(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            return [row[0] for row in conn.execute(
                "SELECT message_id FROM newbie_message_log WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchall()]

    def delete_newbie_messages(self, chat_id: int, user_id: int):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM newbie_message_log WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )

    def add_clan_recruit_memory(self, chat_id: int, player_nickname: str, player_class: str, level: str,
                                accepted_by_user_id: int | None, accepted_by_username: str | None,
                                accepted_by_display_name: str | None):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO clan_recruit_memory(
                    chat_id,player_nickname,player_class,level,
                    accepted_by_user_id,accepted_by_username,accepted_by_display_name,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (chat_id, player_nickname.strip(), player_class.strip(), level.strip(),
                 accepted_by_user_id, accepted_by_username, accepted_by_display_name,
                 datetime.now(timezone.utc).isoformat()),
            )

    def clan_recruits_by_player(self, chat_id: int, player_nickname: str):
        with self._conn() as conn:
            return conn.execute(
                """SELECT player_nickname,player_class,level,accepted_by_username,accepted_by_display_name
                   FROM clan_recruit_memory
                   WHERE chat_id=? AND lower(player_nickname)=lower(?)
                   ORDER BY id DESC""",
                (chat_id, player_nickname.strip()),
            ).fetchall()
