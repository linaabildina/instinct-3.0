import asyncio
import os
import sqlite3
import shutil
import json
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, filedialog

from .config import save_local_settings, ENV_PATH, load_settings
# Явный импорт для PyInstaller: main.py загружается динамически из GUI,
# поэтому зависимости main.py иначе могут не попасть в собранный EXE.
from .storage import Storage  # noqa: F401


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Instinct Bot 3.0")
        self.geometry("1280x1100")
        self.minsize(1100, 900)
        self.running = False
        self.fields = {}
        self.status_vars = {}
        self.status_detail_vars = {}
        self._build()

    def _field(self, parent, label, key, secret=False, default=""):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=12, pady=4)
        ttk.Label(row, text=label, width=28).pack(side="left")
        var = tk.StringVar(value=os.getenv(key, default))
        self.fields[key] = var
        ttk.Entry(row, textvariable=var, show="*" if secret else "").pack(side="left", fill="x", expand=True)

    def _connection_card(self, parent, key, title, subtitle, command, icon):
        column = len(self.status_vars)
        card = ttk.Frame(parent, relief="groove", padding=10)
        card.grid(row=0, column=column, sticky="nsew", padx=5, pady=5)
        parent.columnconfigure(column, weight=1)
        self.status_vars[key] = tk.StringVar(value="⚪ Не проверено")
        self.status_detail_vars[key] = tk.StringVar(value=subtitle)
        ttk.Label(card, text=icon, font=("Segoe UI Symbol", 25)).pack(anchor="w")
        ttk.Label(card, text=title, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(card, textvariable=self.status_vars[key], font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(3, 0))
        ttk.Label(card, textvariable=self.status_detail_vars[key], wraplength=190).pack(anchor="w", pady=(2, 8))
        ttk.Button(card, text="Проверить", command=command).pack(fill="x")

    def _section(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.pack(fill="x", padx=14, pady=6)
        return frame

    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=6)

        self.geometry("1050x760")
        self.minsize(900, 650)

        header = ttk.Frame(self, padding=(10, 7, 10, 2))
        header.pack(fill="x")
        left = ttk.Frame(header)
        left.pack(side="left")
        ttk.Label(left, text="INSTINCT BOT 3.0", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Telegram + OpenAI + Google Docs").pack(anchor="w")
        ttk.Button(header, text="⚙ Настройки", command=self.focus_settings).pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=7, pady=(3, 7))

        overview = ttk.Frame(nb, padding=6)
        sources = ttk.Frame(nb, padding=6)
        settings = ttk.Frame(nb, padding=6)
        prompts = ttk.Frame(nb, padding=6)
        initiative = ttk.Frame(nb, padding=6)
        journal = ttk.Frame(nb, padding=6)
        recruits = ttk.Frame(nb, padding=6)
        memory = ttk.Frame(nb, padding=6)
        for frame, title in [
            (overview, "Обзор"), (sources, "📚 Источники"), (settings, "Настройки"),
            (prompts, "Промты"), (initiative, "Инициативный диалог"), (journal, "Журнал"),
            (recruits, "👤 Новички"), (memory, "🧠 Память Алины")
        ]:
            nb.add(frame, text=title)

        # --- Обзор: подключения + управление ---
        conn = ttk.LabelFrame(overview, text="Подключения и статус", padding=6)
        conn.pack(fill="x", pady=(0, 6))
        cards = ttk.Frame(conn)
        cards.pack(fill="x")
        self._connection_card(cards, "telegram", "Telegram Bot", "Бот не проверен", self.check_telegram, "✈")
        self._connection_card(cards, "group", "Telegram-группа", "ID не проверен", self.check_group, "👥")
        self._connection_card(cards, "openai", "OpenAI", "Модель не проверена", self.check_openai, "◉")
        self._connection_card(cards, "google", "Google Docs", "База знаний не проверена", self.check_google_docs, "◆")

        control = ttk.LabelFrame(overview, text="Управление ботом", padding=6)
        control.pack(fill="x", pady=(0, 6))
        buttons = ttk.Frame(control)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="▶ Запустить", style="Action.TButton", command=self.start).pack(side="left", fill="x", expand=True, padx=(0, 3))
        ttk.Button(buttons, text="■ Остановить", style="Action.TButton", command=self.stop).pack(side="left", fill="x", expand=True, padx=3)
        ttk.Button(buttons, text="↻ Обновить Docs", style="Action.TButton", command=self.refresh).pack(side="left", fill="x", expand=True, padx=(3, 0))
        status = ttk.Frame(control, relief="groove", padding=7)
        status.pack(fill="x", pady=(6, 0))
        self.status = ttk.Label(status, text="● Бот остановлен", font=("Segoe UI", 10, "bold"))
        self.status.pack(side="left")
        self.last_activity = ttk.Label(status, text="Последняя активность: —")
        self.last_activity.pack(side="right")

        # --- Источники ---
        sources.columnconfigure(1, weight=1)
        ttk.Label(sources, text="Сайт:").grid(row=0, column=0, sticky="w", padx=(2, 8), pady=4)
        self.source_url = tk.StringVar(value=os.getenv("KNOWLEDGE_SOURCE_URL", ""))
        ttk.Entry(sources, textvariable=self.source_url).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(sources, text="Форум:").grid(row=1, column=0, sticky="w", padx=(2, 8), pady=4)
        self.sitemap_url = tk.StringVar(value=os.getenv("KNOWLEDGE_SITEMAP_URL", ""))
        ttk.Entry(sources, textvariable=self.sitemap_url).grid(row=1, column=1, sticky="ew", pady=4)
        row = ttk.Frame(sources)
        row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        ttk.Button(row, text="🔎 Сканировать сейчас", command=self.scan_sources).pack(side="left")
        self.source_status = tk.StringVar(value="⚪ Источники не проверены")
        ttk.Label(row, textvariable=self.source_status).pack(side="left", padx=10)
        self.source_error = tk.StringVar(value="")
        ttk.Label(sources, textvariable=self.source_error, foreground="red", wraplength=900).grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        page_frame = ttk.LabelFrame(sources, text="Загруженные страницы", padding=4)
        page_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        sources.rowconfigure(4, weight=1)
        page_frame.columnconfigure(0, weight=1)
        page_frame.rowconfigure(0, weight=1)
        self.source_pages = ttk.Treeview(page_frame, columns=("type", "url"), show="headings", height=8)
        self.source_pages.heading("type", text="Источник")
        self.source_pages.heading("url", text="Страница")
        self.source_pages.column("type", width=90, stretch=False)
        self.source_pages.column("url", width=780)
        self.source_pages.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(page_frame, orient="vertical", command=self.source_pages.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.source_pages.configure(yscrollcommand=sb.set)

        # --- Настройки ---
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)
        self._field(settings, "Telegram Bot Token", "TELEGRAM_BOT_TOKEN", True)
        self._field(settings, "OpenAI API Key", "OPENAI_API_KEY", True)
        self._field(settings, "ID Telegram-группы", "GROUP_CHAT_ID")
        self._field(settings, "Модель OpenAI", "OPENAI_MODEL", default="gpt-5.6-luna")
        self._field(settings, "Google Docs — база знаний", "GOOGLE_DOCS_URL")
        ttk.Button(settings, text="💾 Сохранить настройки", command=self.save).pack(anchor="e", pady=(5, 0))

        # --- Промты ---
        prompts.columnconfigure(0, weight=1)
        prompts.columnconfigure(1, weight=1)
        p1 = ttk.Frame(prompts); p1.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        p2 = ttk.Frame(prompts); p2.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        prompts.rowconfigure(0, weight=1)
        ttk.Label(p1, text="Основной промт").pack(anchor="w")
        self.system_prompt = tk.Text(p1, height=14, wrap="word")
        self.system_prompt.pack(fill="both", expand=True, pady=3)
        self.system_prompt.insert("1.0", os.getenv("SYSTEM_PROMPT", ""))
        ttk.Label(p2, text="Промт инициативы").pack(anchor="w")
        self.initiative_prompt = tk.Text(p2, height=14, wrap="word")
        self.initiative_prompt.pack(fill="both", expand=True, pady=3)
        self.initiative_prompt.insert("1.0", os.getenv("INITIATIVE_PROMPT", ""))

        # --- Инициатива ---
        initiative.columnconfigure(0, weight=1)
        ttk.Label(initiative, text="Автоматические сообщения").grid(row=0, column=0, sticky="w", pady=(2, 8))
        row = ttk.Frame(initiative); row.grid(row=1, column=0, sticky="ew")
        self.enabled = tk.BooleanVar(value=os.getenv("INITIATIVE_ENABLED", "true").lower() == "true")
        ttk.Checkbutton(row, text="Включить инициативные сообщения", variable=self.enabled).pack(side="left")
        ttk.Label(row, text="Интервал (мин.):").pack(side="left", padx=(20, 5))
        self.interval = tk.IntVar(value=max(1, int(os.getenv("INITIATIVE_INTERVAL_MINUTES", "60"))))
        ttk.Spinbox(row, from_=1, to=1440, textvariable=self.interval, width=7).pack(side="left")
        ttk.Button(row, text="✈ Тестовое сообщение", command=self.test_message).pack(side="right")

        # --- Журнал ---
        self.log = tk.Text(journal, height=20, bg="#101214", fg="#e8edf2", insertbackground="white", relief="flat")
        self.log.pack(fill="both", expand=True)
        log_bottom = ttk.Frame(journal); log_bottom.pack(fill="x", pady=(5, 0))
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_bottom, text="Автопрокрутка", variable=self.autoscroll).pack(side="left")
        ttk.Button(log_bottom, text="🗑 Очистить лог", command=self.clear_log).pack(side="right")
        # --- Новички ---
        recruits.columnconfigure(0, weight=1)
        recruits.rowconfigure(1, weight=1)

        recruits_top = ttk.Frame(recruits)
        recruits_top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(
            recruits_top,
            text="Принятые новички",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")
        self.recruits_count = tk.StringVar(value="Всего: 0")
        ttk.Label(recruits_top, textvariable=self.recruits_count).pack(side="left", padx=12)
        ttk.Button(
            recruits_top,
            text="↻ Обновить",
            command=self.refresh_recruits,
        ).pack(side="right")

        recruits_table = ttk.Frame(recruits)
        recruits_table.grid(row=1, column=0, sticky="nsew")
        recruits_table.columnconfigure(0, weight=1)
        recruits_table.rowconfigure(0, weight=1)

        columns = (
            "nickname", "level", "class", "teamspeak",
            "telegram", "accepted_by", "date"
        )
        self.recruits_tree = ttk.Treeview(
            recruits_table,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "nickname": "Игровой ник",
            "level": "Уровень",
            "class": "Класс",
            "teamspeak": "TeamSpeak",
            "telegram": "Telegram",
            "accepted_by": "Кто принял",
            "date": "Дата",
        }
        widths = {
            "nickname": 180, "level": 80, "class": 130,
            "teamspeak": 100, "telegram": 100,
            "accepted_by": 160, "date": 150,
        }
        for column in columns:
            self.recruits_tree.heading(column, text=headings[column])
            self.recruits_tree.column(column, width=widths[column], minwidth=70, stretch=True)

        y_scroll = ttk.Scrollbar(recruits_table, orient="vertical", command=self.recruits_tree.yview)
        x_scroll = ttk.Scrollbar(recruits_table, orient="horizontal", command=self.recruits_tree.xview)
        self.recruits_tree.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )
        self.recruits_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        self._build_memory_tab(memory)

        # При каждом запуске сразу открываем вкладку «Новички».
        # Данные в ней уже загружаются из локальной базы.
        nb.select(recruits)

        self.write("[Система] Приложение запущено")
        self.write("[Система] Все настройки загружены")
        self.write("[Статус] Бот остановлен")
        self.refresh_recruits()

    def refresh_recruits(self):
        """Показывает локально сохранённых новичков из SQLite."""
        try:
            settings = load_settings()
            storage = Storage(settings.db_path)
            rows = storage.all_recruits()

            for item in self.recruits_tree.get_children():
                self.recruits_tree.delete(item)

            for nickname, level, class_name, teamspeak, telegram, username, display_name, created_at in rows:
                accepted_by = f"@{username}" if username else (display_name or "—")
                try:
                    date_text = datetime.fromisoformat(created_at).astimezone().strftime("%d.%m.%Y %H:%M")
                except Exception:
                    date_text = (created_at or "")[:16].replace("T", " ")

                self.recruits_tree.insert(
                    "",
                    "end",
                    values=(
                        nickname or "—",
                        level or "—",
                        class_name or "—",
                        teamspeak or "—",
                        telegram or "—",
                        accepted_by,
                        date_text,
                    ),
                )

            self.recruits_count.set(f"Всего: {len(rows)}")
            self.write(f"[Новички] Загружено записей: {len(rows)}")
        except Exception as exc:
            self.recruits_count.set("Ошибка загрузки")
            self.write(f"[Новички] Ошибка: {exc}")

    # --- Управление памятью Алины (SQLite) ---
    def _build_memory_tab(self, parent):
        """Вкладка управления реальными SQLite-базами Алины."""
        parent.configure()
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="База данных:").pack(side="left")
        from .config import APP_DATA, load_settings
        try:
            settings = load_settings()
            default_bot_db = settings.db_path
        except Exception:
            default_bot_db = str(APP_DATA / "bot.sqlite3")
        self.memory_db_var = tk.StringVar(value="Клановая память")
        self.memory_db_paths = {
            "Клановая память": str(APP_DATA / "clan_memory.sqlite3"),
            "База бота (сообщения и профили)": default_bot_db,
        }
        self.memory_db_combo = ttk.Combobox(
            top, textvariable=self.memory_db_var, state="readonly",
            values=list(self.memory_db_paths), width=34
        )
        self.memory_db_combo.pack(side="left", padx=(6, 12))
        self.memory_db_combo.bind("<<ComboboxSelected>>", lambda _e: self._memory_refresh_tables())
        ttk.Label(top, text="Таблица:").pack(side="left")
        self.memory_table_var = tk.StringVar()
        self.memory_table_combo = ttk.Combobox(
            top, textvariable=self.memory_table_var, state="readonly", width=27
        )
        self.memory_table_combo.pack(side="left", padx=6)
        self.memory_table_combo.bind("<<ComboboxSelected>>", lambda _e: self._memory_refresh_rows())
        ttk.Button(top, text="↻ Обновить", command=self._memory_refresh_tables).pack(side="left", padx=4)

        search_row = ttk.Frame(parent)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="Поиск:").pack(side="left")
        self.memory_search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self.memory_search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=6)
        search_entry.bind("<Return>", lambda _e: self._memory_refresh_rows())
        ttk.Button(search_row, text="Найти", command=self._memory_refresh_rows).pack(side="left")

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True)
        self.memory_tree = ttk.Treeview(table_frame, show="headings", selectmode="browse")
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=self.memory_tree.yview)
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.memory_tree.xview)
        self.memory_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.memory_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", pady=(7, 0))
        ttk.Button(buttons, text="＋ Добавить запись", command=self._memory_add).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="✎ Редактировать", command=self._memory_edit).pack(side="left", padx=5)
        ttk.Button(buttons, text="🗑 Удалить", command=self._memory_delete).pack(side="left", padx=5)
        ttk.Button(buttons, text="📜 История изменений", command=self._memory_history).pack(side="right", padx=5)
        ttk.Button(buttons, text="📦 Резервная копия", command=self._memory_backup).pack(side="right", padx=5)
        self.memory_status = tk.StringVar(value="Выбери базу и таблицу.")
        ttk.Label(parent, textvariable=self.memory_status).pack(anchor="w", pady=(5, 0))
        self._memory_refresh_tables()

    def _memory_db_path(self):
        return self.memory_db_paths[self.memory_db_var.get()]

    def _memory_connect(self):
        path = self._memory_db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _memory_audit(self, action, table_name, row_id, details):
        audit_path = os.path.join(os.path.dirname(self._memory_db_path()), "alina_memory_audit.sqlite3")
        with sqlite3.connect(audit_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL, action TEXT NOT NULL,
                database_path TEXT NOT NULL, table_name TEXT NOT NULL,
                row_key TEXT, details TEXT
            )""")
            conn.execute(
                "INSERT INTO changes(changed_at,action,database_path,table_name,row_key,details) VALUES(?,?,?,?,?,?)",
                (datetime.now().astimezone().isoformat(timespec="seconds"), action,
                 self._memory_db_path(), table_name, str(row_id), str(details)[:4000])
            )

    def _memory_refresh_tables(self):
        try:
            with self._memory_connect() as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            names = [r["name"] for r in rows]
            self.memory_table_combo["values"] = names
            preferred = [n for n in names if any(x in n.lower() for x in
                         ("clan_memory", "player_profiles", "user_memory", "messages", "persona"))]
            if self.memory_table_var.get() not in names:
                self.memory_table_var.set(preferred[0] if preferred else (names[0] if names else ""))
            self._memory_refresh_rows()
        except Exception as exc:
            self.memory_status.set(f"Ошибка чтения базы: {exc}")

    def _memory_columns(self, conn, table):
        safe = table.replace('"', '""')
        return [dict(row) for row in conn.execute(f'PRAGMA table_info("{safe}")').fetchall()]

    def _memory_refresh_rows(self):
        table = self.memory_table_var.get()
        if not table:
            return
        try:
            with self._memory_connect() as conn:
                cols = self._memory_columns(conn, table)
                names = [c["name"] for c in cols]
                sql = f'SELECT * FROM "{table.replace(chr(34), chr(34)*2)}"'
                query = self.memory_search_var.get().strip()
                params = []
                if query and names:
                    sql += " WHERE " + " OR ".join(
                        f'CAST("{n.replace(chr(34), chr(34)*2)}" AS TEXT) LIKE ?' for n in names
                    )
                    params = [f"%{query}%"] * len(names)
                sql += " ORDER BY 1 DESC LIMIT 1000"
                rows = conn.execute(sql, params).fetchall()
            self.memory_tree.delete(*self.memory_tree.get_children())
            self.memory_tree["columns"] = names
            for col in names:
                self.memory_tree.heading(col, text=col)
                self.memory_tree.column(col, width=180, minwidth=90, stretch=True)
            for row in rows:
                values = ["" if row[n] is None else str(row[n]) for n in names]
                self.memory_tree.insert("", "end", iid=str(len(self.memory_tree.get_children())), values=values)
            self.memory_status.set(f"Таблица: {table} · записей показано: {len(rows)} · база: {os.path.basename(self._memory_db_path())}")
        except Exception as exc:
            self.memory_status.set(f"Ошибка чтения таблицы: {exc}")

    def _memory_selected_row(self):
        selected = self.memory_tree.selection()
        if not selected:
            messagebox.showinfo("Память Алины", "Сначала выбери запись в таблице.")
            return None
        table = self.memory_table_var.get()
        values = self.memory_tree.item(selected[0], "values")
        cols = list(self.memory_tree["columns"])
        return dict(zip(cols, values))

    def _memory_editor(self, title, existing=None):
        table = self.memory_table_var.get()
        if not table:
            return
        with self._memory_connect() as conn:
            cols = self._memory_columns(conn, table)
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("620x680")
        win.configure(bg="#101827")
        win.transient(self)
        win.grab_set()
        canvas = tk.Canvas(win, bg="#101827", highlightthickness=0)
        scroll = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=12)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        entries = {}
        pk_cols = [c for c in cols if c.get("pk")]
        for col in cols:
            name = col["name"]
            if existing and col in pk_cols:
                ttk.Label(inner, text=f"{name} (ключ)").pack(anchor="w", pady=(8, 1))
                ttk.Label(inner, text=str(existing.get(name, ""))).pack(anchor="w")
                continue
            ttk.Label(inner, text=name).pack(anchor="w", pady=(8, 1))
            value = "" if not existing or existing.get(name) is None else str(existing.get(name, ""))
            widget = tk.Text(inner, height=4 if len(value) > 100 else 2, wrap="word",
                             bg="#182238", fg="#eef2ff", insertbackground="white",
                             relief="flat", padx=7, pady=5)
            widget.pack(fill="x")
            widget.insert("1.0", value)
            entries[name] = widget

        def save_record():
            data = {k: w.get("1.0", "end-1c").strip() for k, w in entries.items()}
            try:
                with self._memory_connect() as conn:
                    if existing:
                        where_cols = [c for c in pk_cols]
                        if not where_cols:
                            raise ValueError("У таблицы нет первичного ключа: безопасное редактирование невозможно.")
                        where = " AND ".join(f'"{c["name"]}"=?' for c in where_cols)
                        old_key = [existing.get(c["name"]) for c in where_cols]
                        updates = {k: v for k, v in data.items() if k not in [c["name"] for c in pk_cols]}
                        if updates:
                            set_sql = ", ".join(f'"{k}"=?' for k in updates)
                            conn.execute(f'UPDATE "{table}" SET {set_sql} WHERE {where}', list(updates.values()) + old_key)
                        self._memory_audit("UPDATE", table, old_key, json.dumps(data, ensure_ascii=False))
                    else:
                        insert_data = {}
                        for c in cols:
                            name = c["name"]
                            if c.get("pk") and "INT" in (c.get("type") or "").upper() and not data.get(name):
                                continue
                            if name in data:
                                insert_data[name] = data[name]
                        names = list(insert_data)
                        if not names:
                            raise ValueError("Нет полей для сохранения.")
                        conn.execute(
                            f'INSERT INTO "{table}" ({",".join(chr(34)+n+chr(34) for n in names)}) VALUES ({",".join("?" for _ in names)})',
                            [insert_data[n] for n in names]
                        )
                        self._memory_audit("INSERT", table, "", json.dumps(insert_data, ensure_ascii=False))
                win.destroy()
                self._memory_refresh_rows()
            except Exception as exc:
                messagebox.showerror("Не удалось сохранить", str(exc), parent=win)

        ttk.Button(inner, text="💾 Сохранить", command=save_record).pack(anchor="e", pady=14)

    def _memory_add(self):
        self._memory_editor("Добавить запись")

    def _memory_edit(self):
        row = self._memory_selected_row()
        if row is not None:
            self._memory_editor("Редактировать запись", row)

    def _memory_delete(self):
        row = self._memory_selected_row()
        if row is None:
            return
        table = self.memory_table_var.get()
        with self._memory_connect() as conn:
            cols = self._memory_columns(conn, table)
            pk_cols = [c for c in cols if c.get("pk")]
            if not pk_cols:
                messagebox.showerror("Удаление", "У таблицы нет первичного ключа.")
                return
            keys = [row.get(c["name"]) for c in pk_cols]
            if not messagebox.askyesno("Удалить запись", "Удалить выбранную запись без возможности отмены? Сначала убедись, что есть резервная копия."):
                return
            where = " AND ".join(f'"{c["name"]}"=?' for c in pk_cols)
            old = conn.execute(f'SELECT * FROM "{table}" WHERE {where}', keys).fetchone()
            conn.execute(f'DELETE FROM "{table}" WHERE {where}', keys)
            self._memory_audit("DELETE", table, keys, json.dumps(dict(old) if old else row, ensure_ascii=False))
        self._memory_refresh_rows()

    def _memory_history(self):
        from .config import APP_DATA
        audit_paths = {
            os.path.join(os.path.dirname(path), "alina_memory_audit.sqlite3")
            for path in set(self.memory_db_paths.values())
        }
        audit_paths.add(str(APP_DATA / "alina_memory_audit.sqlite3"))
        win = tk.Toplevel(self)
        win.title("История изменений памяти")
        win.geometry("900x480")
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("date", "action", "table", "key", "details"), show="headings")
        for col, label, width in [
            ("date", "Дата", 160), ("action", "Действие", 90), ("table", "Таблица", 160),
            ("key", "Ключ записи", 100), ("details", "Подробности", 420)
        ]:
            tree.heading(col, text=label)
            tree.column(col, width=width, stretch=True)
        tree.pack(fill="both", expand=True)
        count = 0
        for path in audit_paths:
            if not os.path.isfile(path):
                continue
            try:
                with sqlite3.connect(path) as conn:
                    rows = conn.execute(
                        "SELECT changed_at, action, table_name, row_key, details FROM changes ORDER BY id DESC LIMIT 500"
                    ).fetchall()
                for row in rows:
                    tree.insert("", "end", values=row)
                    count += 1
            except sqlite3.Error:
                continue
        ttk.Label(frame, text=f"Операций из журнала панели: {count}").pack(anchor="w", pady=(6, 0))

    def _memory_backup(self):
        from .config import APP_DATA, load_settings
        target_dir = filedialog.askdirectory(title="Выбери папку для резервной копии")
        if not target_dir:
            return
        paths = set(self.memory_db_paths.values())
        paths.add(str(APP_DATA / "clan_memory.sqlite3"))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        copied = []
        try:
            for path in paths:
                if os.path.isfile(path):
                    target = os.path.join(target_dir, f"{os.path.splitext(os.path.basename(path))[0]}_backup_{stamp}.sqlite3")
                    with sqlite3.connect(path) as source, sqlite3.connect(target) as dest:
                        source.backup(dest)
                    copied.append(target)
            if not copied:
                raise FileNotFoundError("Не найдено баз для копирования.")
            messagebox.showinfo("Резервная копия", "Созданы копии:\n" + "\n".join(copied))
        except Exception as exc:
            messagebox.showerror("Резервная копия", str(exc))

    def scan_sources(self):
        site_url = self.source_url.get().strip()
        forum_url = self.sitemap_url.get().strip()
        urls = []
        if site_url:
            urls.append(site_url)
        if forum_url:
            urls.append(forum_url)
        if not urls:
            messagebox.showwarning("Источники знаний", "Укажите сайт или форум.")
            return
        self.save(quiet=True)
        self.source_status.set("🟡 Сканирование...")
        self.source_error.set("")
        self.write("[Источники] Начато сканирование сайта и форума")
        def worker():
            try:
                from .source_sync import collect_sources
                def progress(root, loaded, seen, pending, error, current_url=""):
                    label = "Форум" if "forum." in root.lower() or "/forum" in root.lower() else "Сайт"
                    msg = f"🟡 {label}: загружено {loaded} | найдено ссылок: {seen} | очередь: {pending}"
                    if error:
                        msg += f" | ошибка: {error[:100]}"
                    self.after(0, lambda msg=msg: self.source_status.set(msg))
                    if current_url and not error:
                        self.after(0, lambda u=current_url, l=label: self._add_source_page(l, u))
                    if error:
                        self.after(0, lambda msg=msg: self.write(f"[Источники] {label}: {msg}"))
                self.after(0, lambda: [self.source_pages.delete(x) for x in self.source_pages.get_children()])
                total, details = collect_sources(urls, max_pages=20000, progress=progress)
                summary = " | ".join(
                    f"{'Форум' if ('forum.' in root.lower() or '/forum' in root.lower()) else 'Сайт'}: {count}"
                    for root, count, _ in details
                )
                self.after(0, lambda: self.source_status.set(f"🟢 Загружено страниц: {total}"))
                if forum_url:
                    summary += " | Форум: поиск по поисковику при вопросе"
                self.after(0, lambda: self.source_error.set(summary))
                self.after(0, lambda: self.write(f"[Источники] Сканирование завершено: {summary}"))
            except Exception as e:
                self.after(0, lambda: self.source_status.set("🔴 Ошибка"))
                self.after(0, lambda e=e: self.source_error.set(f"Ошибка сканирования: {e}"))
                self.after(0, lambda e=e: self.write(f"[Источники] Ошибка: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _add_source_page(self, label, url):
        if not hasattr(self, "source_pages"):
            return
        # Не забиваем интерфейс тысячами строк: показываем последние 300 страниц.
        existing = {self.source_pages.item(i, "values")[1] for i in self.source_pages.get_children()}
        if url in existing:
            return
        self.source_pages.insert("", "end", values=(label, url))
        rows = self.source_pages.get_children()
        if len(rows) > 300:
            self.source_pages.delete(rows[0])

    def focus_settings(self):
        self.write("[Система] Раздел настроек доступен ниже")

    def clear_log(self):
        self.log.delete("1.0", "end")

    def test_message(self):
        self.write("[Тест] Тестовое сообщение вызвано")

    def write(self, msg):
        self.log.insert("end", msg + "\n")
        if self.autoscroll.get():
            self.log.see("end")

    def set_status(self, key, text, detail=None):
        if key not in self.status_vars:
            return
        self.after(0, lambda: self.status_vars[key].set(text))
        if detail is not None:
            self.after(0, lambda: self.status_detail_vars[key].set(detail))

    def save(self, quiet=False):
        values = {key: var.get().strip() for key, var in self.fields.items()}
        values["INITIATIVE_ENABLED"] = "true" if self.enabled.get() else "false"
        values["INITIATIVE_INTERVAL_MINUTES"] = str(max(1, int(self.interval.get())))
        values["KNOWLEDGE_REFRESH_MINUTES"] = "10"
        values["KNOWLEDGE_SOURCE_URL"] = self.source_url.get().strip()
        values["KNOWLEDGE_SITEMAP_URL"] = self.sitemap_url.get().strip()
        values["SYSTEM_PROMPT"] = self.system_prompt.get("1.0", "end-1c")
        values["INITIATIVE_PROMPT"] = self.initiative_prompt.get("1.0", "end-1c")
        save_local_settings(values)
        self.write("✓ Настройки сохранены локально.")
        if not quiet:
            messagebox.showinfo("Instinct Bot", "Настройки сохранены.")

    def _run_check(self, name, worker):
        self.set_status(name, "🟡 Проверка...", "Идёт проверка подключения")
        threading.Thread(target=self._check_worker, args=(name, worker), daemon=True).start()

    def _check_worker(self, name, worker):
        try:
            result = worker()
            self.set_status(name, "🟢 Подключен", result)
            self.after(0, lambda: self.write(f"✓ {name}: {result}"))
        except Exception as e:
            self.set_status(name, "🔴 Ошибка", str(e)[:120])
            self.after(0, lambda: self.write(f"✗ {name}: {e}"))

    def check_telegram(self):
        self.save(quiet=True)
        def worker():
            from aiogram import Bot
            from .config import load_settings
            async def check():
                settings = load_settings()
                bot = Bot(settings.telegram_token)
                try:
                    me = await bot.get_me()
                    return f"Бот: @{me.username or me.first_name}"
                finally:
                    await bot.session.close()
            return asyncio.run(check())
        self._run_check("telegram", worker)

    def check_group(self):
        self.save(quiet=True)
        def worker():
            from aiogram import Bot
            from .config import load_settings
            async def check():
                settings = load_settings()
                bot = Bot(settings.telegram_token)
                try:
                    chat = await bot.get_chat(settings.group_chat_id)
                    return f"ID: {chat.id} • {chat.title or 'Чат'}"
                finally:
                    await bot.session.close()
            return asyncio.run(check())
        self._run_check("group", worker)

    def check_openai(self):
        self.save(quiet=True)
        def worker():
            from openai import OpenAI
            from .config import load_settings
            settings = load_settings()
            model = self.fields["OPENAI_MODEL"].get().strip() or settings.openai_model
            OpenAI(api_key=settings.openai_key).models.retrieve(model)
            return f"Модель: {model}"
        self._run_check("openai", worker)

    def check_google_docs(self):
        self.save(quiet=True)
        def worker():
            from .knowledge import refresh_google_doc
            url = self.fields["GOOGLE_DOCS_URL"].get().strip()
            if not url:
                raise ValueError("Ссылка Google Docs не указана")
            if not refresh_google_doc(url):
                raise RuntimeError("Не удалось загрузить Google Docs")
            return "База знаний обновлена"
        self._run_check("google", worker)

    def refresh(self):
        self.check_google_docs()

    def start(self):
        if self.running:
            self.write("[Бот] Уже запущен")
            return
        try:
            self.save(quiet=True)
            self.running = True
            self.status.configure(text="●  Бот запускается...")
            self.write("▶ Запуск бота...")
            threading.Thread(target=self._run_bot, daemon=True).start()
        except Exception as e:
            self.running = False
            self.status.configure(text="●  Ошибка")
            messagebox.showerror("Ошибка", str(e))

    def _run_bot(self):
        try:
            from .main import main

            async def run():
                await main()

            asyncio.run(run())
        except Exception as e:
            self.after(0, lambda: self.write(f"✗ Ошибка бота: {e}"))
            self.after(0, lambda: self.status.configure(text="●  Ошибка"))
        finally:
            self.running = False

    def stop(self):
        if not self.running:
            self.write("[Бот] Уже остановлен")
            return
        self.write("⏹ Остановка запрошена.")
        self.status.configure(text="●  Останавливается...")
        try:
            from .main import request_stop
            request_stop()
        except Exception as e:
            self.write(f"✗ Ошибка остановки: {e}")


if __name__ == "__main__":
    App().mainloop()
