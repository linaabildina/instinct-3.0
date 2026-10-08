import asyncio
import json
import sys
import logging
import re
import random
from html import escape
from pathlib import Path
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeChatAdministrators,
    BotCommandScopeChatMember, BotCommandScopeDefault,
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji, WebAppInfo,
)

from app.ai import AIEngine
from app.config import load_settings
from app.storage import Storage
from app.roulette import (spun_today, cooldown_remaining, get_pending, save_pending, make_token, verify_token, save_spin, claim_spin, choose_prize_id, roulette_stats)
from app.reminders import ReminderService, parse_command
from app.knowledge_ui import router as knowledge_router
from app.source_sync import collect_sources
from app.game_features import init_game_features, add_watch, list_watches, remove_watch, check_watches
from app.forum_search import search_forum
from app.youtube_monitor import monitor_forever, _latest_video
from app.youtube_browser import like_video, get_video_rating
from app.roles import RoleManager, is_telegram_admin
from app.google_newbie import send_newbie_to_google, test_google_newbie, get_google_config_status
try:
    from app.news_monitor import run_news_monitor_in_thread
except ImportError:
    run_news_monitor_in_thread = None

# Логирование одновременно в консоль и в отдельный файл logs/bot.log.
# Логи всегда создаются рядом с запущенным EXE/скриптом, а не зависят от текущей рабочей папки.
try:
    APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
except Exception:
    APP_DIR = Path.cwd()

LOG_DIR = APP_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
settings = load_settings()
storage = Storage(settings.db_path)
role_manager = RoleManager(settings.db_path)
init_game_features(settings.db_path)
ai = AIEngine(settings.openai_key, settings.openai_model, settings.system_prompt, storage)
reminder_service = ReminderService(settings.db_path)
dp = Dispatcher()
dp.include_router(knowledge_router)

_bot_id: int | None = None
_bot_username: str | None = None
_polling_loop: asyncio.AbstractEventLoop | None = None
_polling_task: asyncio.Task | None = None
_knowledge_sync_task: asyncio.Task | None = None
_news_monitor_thread = None
_watch_task: asyncio.Task | None = None
_reminder_task: asyncio.Task | None = None
_youtube_task: asyncio.Task | None = None
_random_events_task: asyncio.Task | None = None
_morning_greeting_task: asyncio.Task | None = None
_roulette_task: asyncio.Task | None = None

ROULETTE_WEBAPP_URL = "https://linaabildina.github.io/instinct-3.0/telegram-mini-app/roulette.html"
_newbie_sessions: dict[int, dict] = {}
_NEWBIE_YES = {"да", "д", "yes", "y", "конечно"}
_NEWBIE_NO = {"нет", "н", "no", "n"}

_NAME_ADDRESS = re.compile(r'(?i)(?<!\w)алин(?:а|е|у|ой|ы)?(?!\w)')


def _addressed_to_alina(text: str) -> bool: return bool(_NAME_ADDRESS.search(text))


def _mentioned_alina(message: Message) -> bool:
    """Распознаёт прямое Telegram-упоминание бота (@username/text mention)."""
    if not message.entities:
        return False
    for entity in message.entities:
        if entity.type == "text_mention" and entity.user and entity.user.id == _bot_id:
            return True
        if entity.type == "mention" and _bot_username:
            try:
                value = message.parse_entity(entity).lstrip("@").lower()
            except Exception:
                value = ""
            if value == _bot_username.lower():
                return True
    return False


def _is_knowledge_correction(text: str) -> bool:
    normalized = text.lower().strip()
    markers = (
        "ты ошиблась", "ты ошибся", "это неверно", "это неправильно",
        "неправильно", "ошибка", "исправь", "исправление", "на самом деле",
        "правильно будет", "правильный ответ", "не так", "не верно",
    )
    has_marker = any(marker in normalized for marker in markers)
    has_contrast = bool(re.search(r"\\bне\\b.{0,120}\\bа\\b", normalized, re.S))
    return has_marker or has_contrast


def _strip_urls(text: str) -> str:
    text = re.sub(r'\[([^\]]+)\]\(\s*<?https?://[^)>]+>?\s*\)', r'\1', text)
    text = re.sub(r'<https?://[^>]+>', '', text)
    text = re.sub(r'\[\s*https?://[^\]]+\s*\]', '', text)
    text = re.sub(r'https?://[^\s)\]>]+', '', text, flags=re.I)
    text = re.sub(r'(?i)www\.[^\s)\]>]+', '', text)
    text = '\n'.join(line for line in text.splitlines() if not re.search(r'(?i)(https?://|www\.)', line))
    text = re.sub(r'!\[([^\]]*)\]\(\s*\)', r'\1', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\[\s*\]', '', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n[ \t]*\n[ \t]*\n+', '\n\n', text)
    return text.strip()


def _is_allowed_chat(message: Message) -> bool: return message.chat.id == settings.group_chat_id


async def _is_admin_user(message: Message) -> bool:
    if not message.from_user or not _is_allowed_chat(message):
        return False
    return await is_telegram_admin(message.bot, message.chat.id, message.from_user.id)


async def _can_use_newbie(message: Message) -> bool:
    if not message.from_user or not _is_allowed_chat(message):
        return False
    if await is_telegram_admin(message.bot, message.chat.id, message.from_user.id):
        return True
    return role_manager.get_role(message.chat.id, message.from_user.id) == "officer"


async def _is_admin_callback(callback: CallbackQuery) -> bool:
    if not callback.message or not callback.from_user or not _is_allowed_chat(callback.message):
        return False
    return await is_telegram_admin(callback.bot, callback.message.chat.id, callback.from_user.id)


async def _can_use_newbie_callback(callback: CallbackQuery) -> bool:
    if not callback.message or not callback.from_user or not _is_allowed_chat(callback.message):
        return False
    if await is_telegram_admin(callback.bot, callback.message.chat.id, callback.from_user.id):
        return True
    return role_manager.get_role(callback.message.chat.id, callback.from_user.id) == "officer"


async def _close_newbie_session(callback: CallbackQuery, notice: str | None = None):
    user_id = callback.from_user.id
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    message_ids = storage.get_newbie_message_ids(chat_id, user_id)
    ids = set(storage.get_newbie_messages(chat_id, user_id))
    ids.add(callback.message.message_id)
    if message_ids:
        ids.update(x for x in message_ids if x)

    _newbie_sessions.pop(user_id, None)
    storage.delete_newbie_draft(chat_id, user_id)
    storage.delete_newbie_message_ids(chat_id, user_id)
    storage.delete_newbie_messages(chat_id, user_id)

    for message_id in ids:
        try:
            await callback.bot.delete_message(chat_id, message_id)
        except Exception as exc:
            logging.warning("Could not delete newbie message %s: %s", message_id, exc)

    if notice:
        await callback.answer(notice)


@dp.callback_query(F.data == "newbie_cancel")
async def newbie_cancel_callback(callback: CallbackQuery):
    if not await _can_use_newbie_callback(callback):
        await callback.answer("Анкета доступна только администратору или офицеру.", show_alert=True)
        return
    await _close_newbie_session(callback, "Анкета отменена")


@dp.callback_query(F.data == "newbie_reject")
async def newbie_reject_callback(callback: CallbackQuery):
    if not await _can_use_newbie_callback(callback):
        await callback.answer("Анкета доступна только администратору или офицеру.", show_alert=True)
        return
    await _close_newbie_session(callback, "Анкета отклонена")


@dp.callback_query(F.data == "newbie_restart")
async def newbie_restart_callback(callback: CallbackQuery):
    if not await _can_use_newbie_callback(callback):
        await callback.answer("Анкета доступна только администратору или офицеру.", show_alert=True)
        return
    user_id = callback.from_user.id
    if callback.message is None:
        await callback.answer()
        return
    chat_id = callback.message.chat.id
    await _close_newbie_session(callback, "Заполняем заново")
    _newbie_sessions[user_id] = {"chat_id": chat_id, "step": "form", "data": {}}
    storage.save_newbie_draft(chat_id, user_id, {})
    prompt_message = await callback.message.answer(
        "📝 <b>Анкета новичка</b>\n\n"
        "🎮 Игровой ник:\n"
        "⭐ Уровень:\n"
        "⚔️ Класс:\n"
        "🎧 TeamSpeak: Да / Нет\n"
        "📱 Telegram: Да / Нет\n\n"
        "Отправь <b>одним сообщением через запятую</b> в таком порядке:\n"
        "<code>ИгровойНик, 146, Син, Да, Да</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить", callback_data="newbie_cancel"),
        ]]),
        parse_mode="HTML",
    )
    _newbie_sessions[user_id]["questionnaire_message_id"] = prompt_message.message_id
    storage.save_newbie_message_ids(chat_id, user_id, questionnaire_message_id=prompt_message.message_id)


@dp.message(
    F.text,
    lambda message: _is_allowed_chat(message)
    and re.sub(r'[,:!?]+', ' ', (message.text or '').strip()).strip().lower()
    in {'алина', 'алина привет', 'алина ты тут', 'алина ты здесь', 'алина ау', 'алина ало', 'алина откликнись'}
)
async def direct_alina_ping(message: Message):
    """Быстрый ответ на простые обращения, не зависящий от ИИ."""
    await message.answer("Да, я здесь 🙂", reply_to_message_id=message.message_id)


def _is_youtube_question(text: str) -> bool:
    normalized = text.lower()
    return (
        ("ютуб" in normalized or "youtube" in normalized or "k4mui" in normalized)
        and ("лайк" in normalized or "видео" in normalized or "ролик" in normalized)
        and ("провер" in normalized or "точно" in normalized or "постав" in normalized or "лайкнула" in normalized or "лайкнул" in normalized)
    )


async def _youtube_status_answer() -> str:
    latest = await asyncio.to_thread(_latest_video)
    if not latest:
        return "Не смогла проверить последний ролик @k4mui_play."
    video_id, title, url, _published = latest
    rating = await asyncio.to_thread(get_video_rating, video_id)
    if rating == "like":
        return f"Да 😌 Проверила — на последнем ролике «{title}» лайк от моего аккаунта уже стоит."
    if rating == "dislike":
        return f"Проверила 👀 На последнем ролике «{title}» сейчас стоит дизлайк."
    if rating == "none":
        return f"Проверила — на последнем ролике «{title}» лайка от моего аккаунта сейчас нет."
    return f"Проверила — YouTube не показывает, что мой аккаунт поставил лайк на «{title}»."




def _parse_watch_command(text: str):
    body = re.sub(r'^/watch(?:@\w+)?\s*', '', text, flags=re.I).strip()
    if not body: return None, None
    match = re.search(r'\s+(?:до|дешевле|не дороже)\s+([\d\s.,]+)\s*$', body, re.I)
    if match:
        digits = re.sub(r'\D', '', match.group(1))
        return body[:match.start()].strip(), int(digits) if digits else None
    return body, None




@dp.message(F.text, lambda message: _is_allowed_chat(message) and parse_command((message.text or '').strip()) is not None)
async def command_reminder(message: Message):
    if not await _is_admin_user(message):
        return
    username = message.from_user.username if message.from_user else None

    parsed = parse_command((message.text or "").strip())
    if not parsed:
        return

    run_at, reminder_text = parsed
    reminder_id = reminder_service.schedule(
        message.chat.id,
        message.from_user.id if message.from_user else None,
        username,
        reminder_text,
        run_at,
    )
    when = run_at.strftime("%d.%m.%Y в %H:%M")
    await message.answer(f"⏰ Готово. Напомню {when}: {reminder_text} (№{reminder_id})")




@dp.message(F.text.startswith('/reminders'))
async def command_reminders(message: Message):
    if not await _is_admin_user(message):
        return
    rows = reminder_service.list_pending(message.chat.id)
    if not rows:
        await message.answer('📭 Активных напоминаний нет.')
        return
    lines = ['⏰ Активные напоминания:']
    for reminder_id, text, run_at, repeat_rule in rows:
        dt = datetime.fromisoformat(run_at).astimezone()
        lines.append(f'#{reminder_id} — {dt.strftime("%d.%m.%Y %H:%M")} — {text}')
    lines.append('Отменить: /cancel ID')
    await message.answer('\n'.join(lines))


@dp.message(F.text.startswith('/cancel'))
async def command_cancel(message: Message):
    if not await _is_admin_user(message):
        return
    match = re.search(r'^/cancel(?:@\w+)?\s+(\d+)', message.text or '', re.I)
    if not match:
        await message.answer('Формат: /cancel ID')
        return
    ok = reminder_service.cancel(message.chat.id, int(match.group(1)))
    await message.answer('🗑 Напоминание отменено.' if ok else 'Не нашла такое напоминание.')



def _parse_forum_search(text: str):
    body = re.sub(r'(?i)^\s*алина[,:]?\s*', '', text).strip()
    match = re.match(r'(?is)^(?:поищи|найди|ищи|посмотри|проверь)\s+(?:на\s+)?форум(?:е|а)?\s+(.+)$', body)
    if not match:
        return None
    query = match.group(1).strip()
    query = re.sub(r'(?i)\b(?:на\s+)?форуме\b', '', query).strip(' ,:')
    return query or None


@dp.message(F.text, lambda message: _is_allowed_chat(message) and _parse_forum_search((message.text or '').strip()) is not None)
async def command_forum_search(message: Message):
    if not await _is_admin_user(message):
        return
    query = _parse_forum_search((message.text or '').strip())
    if not query:
        return
    sources = [x.strip() for x in settings.knowledge_sources.splitlines() if x.strip()]
    forum_url = next((x for x in sources if 'forum' in x.lower()), sources[0] if sources else '')
    if not forum_url:
        await message.answer('Не настроен адрес форума.')
        return
    try:
        result = await asyncio.to_thread(search_forum, forum_url, query, 7)
        if not result:
            await message.answer(f'🔎 По запросу «{query}» на форуме ничего не нашла.')
            return
        await message.answer(f'🔎 Нашла на форуме по запросу «{query}»:\n\n{result}')
    except Exception as exc:
        logging.exception('Forum search failed: %s', exc)
        await message.answer('Не смогла выполнить поиск по форуму. Попробуй ещё раз.')



def _game_request(text: str) -> bool:
    normalized = re.sub(r'[^a-zа-яё0-9 ]+', ' ', text.lower())
    normalized = re.sub(r'\\s+', ' ', normalized).strip()
    return bool(re.search(r'\\bалина\\b', normalized) and re.search(r'\\bдавай\\s+(?:поиграем|играть)\\b', normalized))


@dp.message(F.text, lambda message: _is_allowed_chat(message) and _game_request((message.text or '').strip()))
async def command_game(message: Message):
    if not await _is_admin_user(message):
        return
    import random
    games = [
        ('💋 «Правда или провокация»', 'Пишем «Я» — я выберу участников и начнём.'),
        ('😈 «Самый наглый»', 'Я даю ситуацию, а вы пишете самый дерзкий ответ. Победителя выберу я.'),
        ('🔥 «Кому бы ты…»', 'Я задаю провокационные вопросы про участников чата. Отвечаем честно 😏'),
        ('🍷 «Свидание вслепую»', 'Я случайно объединю двух участников и устрою им мини-свидание.'),
        ('😏 «Продолжи фразу»', 'Я начинаю фразу, а вы заканчиваете её. Чем смешнее и смелее — тем лучше.'),
        ('😂 «Кто из нас?»', 'Я задаю вопросы вроде «кто первым влюбится?» — а вы выбираете игрока.'),
    ]
    title, rules = random.choice(games)
    await message.answer(
        f'😈 Ну что, начинаем?\\n\\n'
        f'Сегодня я выбрала {title}.\\n\\n'
        f'{rules}\\n\\n'
        f'Кто участвует — пишите «Я» 👀'
    )


@dp.message(F.text.startswith('/consultant'))
async def command_consultant(message: Message):
    if not await _is_admin_user(message): return
    global _bot_id
    if _bot_id is None:
        await message.answer('ИИ-консультант пока не готов.')
        return
    bot = await message.bot.get_me()
    username = bot.username
    if not username:
        await message.answer('У бота нет username, поэтому не могу открыть Mini App.')
        return
    url = f"https://t.me/{username}?startapp=consultant"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🤖 Открыть ИИ-консультанта', url=url)
    ]])
    await message.answer('Откройте мой ИИ-консультант прямо внутри Telegram:', reply_markup=keyboard)



def _newbie_prompt(message: Message) -> str:
    return (
        "📝 Заполняем новичка. Я задам несколько вопросов.\n\n"
        "1️⃣ Игровой ник:"
    )


def _normalize_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@").lower()


def _parse_recruiter_query(text: str):
    normalized = text.strip()
    # «Кого я принял/приняла?» — это запрос самого отправителя.
    if re.search(r"(?iu)\bкого\s+я\s+(?:принял|приняла|принимал|принимала|внёс|внесла|записал|записала)\b", normalized):
        return "__SELF__"
    match = re.search(
        r"(?iu)\bкого\s+(?:ты\s+)?(?:принял|приняла|приняли|принимал|принимала|принимали|внёс|внес|внесла|внесли|записал|записала|записали)\s+@?([a-z0-9_]+)",
        normalized,
    )
    if match:
        return match.group(1)
    return None


@dp.message(
    F.text,
    lambda message: (
        _is_allowed_chat(message)
        and _addressed_to_alina((message.text or "").strip())
        and _parse_recruiter_query((message.text or "").strip()) is not None
    ),
)
async def recruiter_history_query(message: Message):
    if not await _is_admin_user(message):
        return
    # История рекрутирования всегда обрабатывается отдельно от /newbie.
    await command_recruiter_list(message)



_ADMIN_COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="stop", description="Остановить бота"),
    BotCommand(command="status", description="Показать статус"),
    BotCommand(command="newbie", description="Запустить анкету новичка"),
    BotCommand(command="officer", description="Назначить офицером"),
    BotCommand(command="unofficer", description="Снять статус офицера"),
    BotCommand(command="officers", description="Показать список офицеров"),
    BotCommand(command="roles", description="Управление ролями"),
    BotCommand(command="analytics", description="Аналитика набора"),
    BotCommand(command="watch", description="Добавить наблюдение"),
    BotCommand(command="watches", description="Список наблюдений"),
    BotCommand(command="unwatch", description="Удалить наблюдение"),
    BotCommand(command="history", description="История предмета"),
    BotCommand(command="market", description="Проверить рынок"),
    BotCommand(command="reminders", description="Мои напоминания"),
    BotCommand(command="cancel", description="Отменить напоминание"),
    BotCommand(command="consultant", description="Открыть консультанта"),
    BotCommand(command="roulette_stats", description="Статистика рулетки"),
]

_OFFICER_COMMANDS = [
    BotCommand(command="newbie", description="Запустить анкету новичка"),
]


async def _set_commands_for_user(bot: Bot, chat_id: int, user_id: int, role: str):
    scope = BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id)

    # Персональный scope нужен только офицеру.
    # Для участника/администратора его нельзя оставлять пустым:
    # пустой ChatMember scope имеет приоритет над ChatAdministrators.
    if role == "officer":
        await bot.set_my_commands(_OFFICER_COMMANDS, scope=scope)
    else:
        await bot.delete_my_commands(scope=scope)


async def _sync_role_command_menus(bot: Bot):
    chat_id = settings.group_chat_id
    await bot.set_my_commands([], scope=BotCommandScopeChat(chat_id=chat_id))
    await bot.set_my_commands(
        _ADMIN_COMMANDS,
        scope=BotCommandScopeChatAdministrators(chat_id=chat_id),
    )

    # Важно: очищаем персональные scopes у обычных участников и администраторов,
    # чтобы они получали команды из общего Chat/ChatAdministrators scope.
    for user_id, username, display_name, role in role_manager.list_roles(chat_id):
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in {"creator", "administrator"}:
                await bot.delete_my_commands(
                    scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id)
                )
                continue
        except Exception:
            pass
        await _set_commands_for_user(bot, chat_id, user_id, role)


async def _refresh_user_command_menu(bot: Bot, chat_id: int, user_id: int):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in {"creator", "administrator"}:
            await bot.delete_my_commands(
                scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id)
            )
            return
    except Exception:
        pass

    await _set_commands_for_user(
        bot, chat_id, user_id, role_manager.get_role(chat_id, user_id)
    )


async def _resolve_role_target(message: Message):
    """Возвращает (user_id, username, display_name) по reply или @username."""
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return None
        return target.id, target.username, target.full_name or target.first_name

    parts = (message.text or "").split()
    if len(parts) < 2:
        return None

    username = parts[1].strip()
    if not username.startswith("@"):
        return None

    found = role_manager.find_user_by_username(message.chat.id, username)
    return found


@dp.message(Command('officer'))
async def command_officer(message: Message):
    if not await _is_admin_user(message):
        return

    target = await _resolve_role_target(message)
    if not target:
        await message.answer(
            "Использование: <code>/officer @username</code>\n"
            "или ответь командой <code>/officer</code> на сообщение участника.",
            parse_mode="HTML",
        )
        return

    target_id, username, display_name = target
    try:
        member = await message.bot.get_chat_member(message.chat.id, target_id)
        target_user = member.user
        username = target_user.username
        display_name = target_user.full_name or target_user.first_name
    except Exception:
        pass

    if target_id == message.from_user.id:
        await message.answer("Ты уже администратор Telegram. Назначать себя офицером не нужно.")
        return

    try:
        target_member = await message.bot.get_chat_member(message.chat.id, target_id)
        if target_member.status in {"creator", "administrator"}:
            await message.answer("👑 Telegram-администратору отдельная роль офицера не нужна.")
            return
    except Exception:
        pass

    role_manager.set_role(
        message.chat.id,
        target_id,
        "officer",
        username,
        display_name,
    )
    await _refresh_user_command_menu(message.bot, message.chat.id, target_id)
    name = f"@{username}" if username else (display_name or str(target_id))
    await message.answer(f"🛡️ {name} назначен офицером.\nЕдинственная специальная команда: <code>/newbie</code>.", parse_mode="HTML")


@dp.message(Command('unofficer'))
async def command_unofficer(message: Message):
    if not await _is_admin_user(message):
        return

    target = await _resolve_role_target(message)
    if not target:
        await message.answer(
            "Использование: <code>/unofficer @username</code>\n"
            "или ответь командой <code>/unofficer</code> на сообщение участника.",
            parse_mode="HTML",
        )
        return

    target_id, username, display_name = target
    try:
        member = await message.bot.get_chat_member(message.chat.id, target_id)
        target_user = member.user
        username = target_user.username
        display_name = target_user.full_name or target_user.first_name
    except Exception:
        pass

    role_manager.set_role(
        message.chat.id,
        target_id,
        "member",
        username,
        display_name,
    )
    await _refresh_user_command_menu(message.bot, message.chat.id, target_id)
    name = f"@{username}" if username else (display_name or str(target_id))
    await message.answer(f"👤 {name} больше не офицер.")


@dp.message(Command('officers'))
async def command_officers(message: Message):
    if not await _is_admin_user(message):
        return

    rows = role_manager.list_roles(message.chat.id)
    officers = [
        f"@{username}" if username else (display_name or str(user_id))
        for user_id, username, display_name, role in rows
        if role == "officer"
    ]
    await message.answer(
        "🛡️ <b>Офицеры</b>\n\n" +
        ("\n".join(f"• {name}" for name in officers) if officers else "• пока нет"),
        parse_mode="HTML",
    )


@dp.message(Command('roles'))
async def command_roles(message: Message):
    if not await _is_admin_user(message):
        return

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target or target.is_bot:
        rows = role_manager.list_roles(message.chat.id)
        officers = [
            (display_name or (f"@{username}" if username else str(user_id)))
            for user_id, username, display_name, role in rows
            if role == "officer"
        ]
        await message.answer(
            "👥 <b>Роли</b>\n\n"
            "Ответь командой <code>/roles</code> на сообщение участника, чтобы назначить роль.\n\n"
            "⚔️ Офицеры:\n"
            + ("\n".join(f"• {name}" for name in officers) if officers else "• пока нет"),
            parse_mode="HTML",
        )
        return

    current = role_manager.get_role(message.chat.id, target.id)
    current_label = "⚔️ Офицер" if current == "officer" else "🎮 Участник"
    target_name = target.full_name or target.first_name or (f"@{target.username}" if target.username else str(target.id))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Офицер", callback_data=f"roles:officer:{target.id}"),
        InlineKeyboardButton(text="🎮 Участник", callback_data=f"roles:member:{target.id}"),
    ]])
    await message.answer(
        f"👤 <b>{target_name}</b>\nТекущая роль: {current_label}\n\nВыбери роль:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("roles:"))
async def callback_role(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        await callback.answer("Назначать роли может только администратор.", show_alert=True)
        return
    if not callback.message:
        await callback.answer()
        return

    match = re.match(r"^roles:(officer|member):(\d+)$", callback.data or "")
    if not match:
        await callback.answer("Некорректная роль.", show_alert=True)
        return

    role = match.group(1)
    target_id = int(match.group(2))
    try:
        target = (await callback.bot.get_chat_member(callback.message.chat.id, target_id)).user
    except Exception:
        await callback.answer("Не удалось найти участника.", show_alert=True)
        return

    role_manager.set_role(
        callback.message.chat.id,
        target_id,
        role,
        target.username,
        target.full_name or target.first_name,
    )
    label = "⚔️ Офицер" if role == "officer" else "🎮 Участник"
    await callback.answer(f"Назначено: {label}")
    await callback.message.edit_text(
        f"👤 <b>{target.full_name or target.first_name or target_id}</b>\nРоль: {label}",
        parse_mode="HTML",
    )


@dp.message(Command('newbie'))
async def command_newbie(message: Message):
    if not await _can_use_newbie(message):
        return
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
    _newbie_sessions[user_id] = {
        "chat_id": message.chat.id,
        "step": "form",
        "data": {},
    }
    storage.save_newbie_draft(message.chat.id, user_id, {})
    is_admin = await _is_admin_user(message)
    newbie_buttons = [
        InlineKeyboardButton(text="🚫 Отменить", callback_data="newbie_cancel"),
    ]
    if is_admin:
        newbie_buttons.append(
            InlineKeyboardButton(text="☁️ Тест Google Sheets", callback_data="google_sheets_test")
        )

    prompt_message = await message.answer(
        "📝 <b>Анкета новичка</b>\n\n"
        "🎮 Игровой ник:\n"
        "⭐ Уровень:\n"
        "⚔️ Класс:\n"
        "🎧 TeamSpeak: Да / Нет\n"
        "📱 Telegram: Да / Нет\n\n"
        "Отправь <b>одним сообщением через запятую</b> в таком порядке:\n"
        "<code>ИгровойНик, 146, Син, Да, Да</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[newbie_buttons]),
        parse_mode="HTML",
    )
    _newbie_sessions[user_id]["questionnaire_message_id"] = prompt_message.message_id
    storage.save_newbie_message_ids(message.chat.id, user_id, questionnaire_message_id=prompt_message.message_id)
    storage.add_newbie_message(message.chat.id, user_id, prompt_message.message_id)


@dp.message(
    F.text,
    lambda message: bool(message.from_user and message.from_user.id in _newbie_sessions)
    and not (
        _is_allowed_chat(message)
        and _addressed_to_alina((message.text or "").strip())
        and _parse_recruiter_query((message.text or "").strip()) is not None
    ),
)
async def newbie_form_message(message: Message):
    if not await _can_use_newbie(message):
        return
    user_id = message.from_user.id
    text = (message.text or "").strip()

    # Запрос истории всегда имеет приоритет над активной анкетой.
    recruiter = _parse_recruiter_query(text)
    if _is_allowed_chat(message) and _addressed_to_alina(text) and recruiter:
        await command_recruiter_list(message)
        return

    session = _newbie_sessions.get(user_id)
    if not session or session["chat_id"] != message.chat.id:
        draft = storage.get_newbie_draft(message.chat.id, user_id)
        if draft:
            session = {"chat_id": message.chat.id, "step": "form", "data": draft}
            _newbie_sessions[user_id] = session
        else:
            return

    if text.lower().startswith("/newbie"):
        session["step"] = "form"
        session["data"] = {}
        storage.save_newbie_draft(message.chat.id, user_id, {})
        await message.answer(
            "📝 <b>Анкета новичка</b>\n\n"
            "🎮 Игровой ник:\n"
            "⭐ Уровень:\n"
            "⚔️ Класс:\n"
            "🎧 TeamSpeak: Да / Нет\n"
            "📱 Telegram: Да / Нет\n\n"
            "Отправь <b>одним сообщением через запятую</b> в таком порядке:\n"
            "<code>ИгровойНик, 146, Син, Да, Да</code>",
            parse_mode="HTML",
        )
        return

    if session.get("step") == "confirm":
        # Пока пользователь не нажал «Принять» или «Отклонить»,
        # не продолжаем анкету и не отдаём сообщение другим обработчикам.
        data = session.get("data") or storage.get_newbie_draft(message.chat.id, user_id)
        if data and data.get("game_nickname"):
            reminder_message = await message.answer(
                "📋 <b>Анкета ожидает решения.</b>\n\n"
                "Принять анкету или отклонить?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Принять", callback_data="newbie_confirm"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data="newbie_reject"),
                    InlineKeyboardButton(text="🚫 Отменить", callback_data="newbie_cancel"),
                ]]),
                parse_mode="HTML",
            )
            storage.add_newbie_message(message.chat.id, user_id, reminder_message.message_id)
            return

    # Анкету заполняем одним сообщением через запятую:
    # Игровой ник, уровень, класс, TeamSpeak, Telegram
    fields = [part.strip() for part in text.split(",")]
    if len(fields) != 5:
        await message.answer(
            "⚠️ Отправь анкету одним сообщением через запятую:\n\n"
            "<code>Малком, 100, Син, Да, Да</code>\n\n"
            "Порядок: игровой ник, уровень, класс, TeamSpeak, Telegram.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚫 Отменить", callback_data="newbie_cancel"),
            ]]),
            parse_mode="HTML",
        )
        return

    teamspeak = fields[3].lower()
    telegram = fields[4].lower()
    if teamspeak not in _NEWBIE_YES | _NEWBIE_NO or telegram not in _NEWBIE_YES | _NEWBIE_NO:
        await message.answer("⚠️ В строках TeamSpeak и Telegram укажи только «Да» или «Нет».")
        return

    data = {
        "game_nickname": fields[0][:100],
        "level": fields[1][:50],
        "class_name": fields[2][:100],
        "teamspeak": "Да" if teamspeak in _NEWBIE_YES else "Нет",
        "telegram": "Да" if telegram in _NEWBIE_YES else "Нет",
    }
    storage.save_newbie_draft(message.chat.id, user_id, data)
    session["data"] = data
    session["step"] = "confirm"

    confirmation_message = await message.answer(
        "📋 <b>Проверь анкету:</b>\n\n"
        f"🎮 Игровой ник: {data['game_nickname']}\n"
        f"⭐ Уровень: {data['level']}\n"
        f"⚔️ Класс: {data['class_name']}\n"
        f"🎧 TeamSpeak: {data['teamspeak']}\n"
        f"📱 Telegram: {data['telegram']}\n\n"
        "Всё верно?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data="newbie_confirm"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data="newbie_reject"),
        ]]),
        parse_mode="HTML",
    )
    storage.save_newbie_message_ids(
        message.chat.id, user_id, confirmation_message_id=confirmation_message.message_id
    )
    storage.add_newbie_message(message.chat.id, user_id, confirmation_message.message_id)


@dp.callback_query(F.data == "google_sheets_test")
async def callback_google_sheets_test(callback: CallbackQuery):
    if callback.message is None or not _is_allowed_chat(callback.message):
        await callback.answer()
        return

    if not await _is_admin_callback(callback):
        await callback.answer("Эта кнопка доступна только администраторам.", show_alert=True)
        return

    await callback.answer("Проверяю Google Sheets…")
    try:
        ok, details = await asyncio.to_thread(test_google_newbie)
        # Диагностика использует тот же runtime-конфиг, который реально
        # использует google_newbie.py. Сам секрет никогда не показываем.
        webhook, secret_set = await asyncio.to_thread(get_google_config_status)
        url_info = webhook if webhook else "НЕ НАЙДЕН"
        secret_info = "задан" if secret_set else "НЕ ЗАДАН"
        safe_details = escape(str(details))
        safe_url_info = escape(str(url_info))

        if ok:
            text = (
                "☁️ <b>Google Sheets работает</b>\n\n"
                f"{safe_details}.\n\n"
                "<b>Диагностика EXE:</b>\n"
                f"URL: <code>{safe_url_info}</code>\n"
                f"SECRET: {secret_info}\n\n"
                "Проверь строку <b>TEST</b> в таблице."
            )
        else:
            text = (
                "☁️ <b>Тест Google Sheets не пройден</b>\n\n"
                f"{safe_details}\n\n"
                "<b>Диагностика EXE:</b>\n"
                f"URL: <code>{safe_url_info}</code>\n"
                f"SECRET: {secret_info}"
            )
        await callback.message.answer(text, parse_mode="HTML")
    except Exception as exc:
        logging.exception("[NEWBIE][GOOGLE] Test failed")
        safe_error = escape(str(exc))
        await callback.message.answer(
            f"☁️ <b>Тест не пройден</b>\n\nОшибка: <code>{safe_error}</code>",
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "newbie_confirm")
async def newbie_confirm_callback(callback: CallbackQuery):
    if not await _can_use_newbie_callback(callback):
        await callback.answer("Анкета доступна только администратору или офицеру.", show_alert=True)
        return
    user_id = callback.from_user.id
    logging.info("[NEWBIE] Нажата кнопка ПРИНЯТЬ: user_id=%s", user_id)
    if callback.message is None:
        await callback.answer("Не удалось найти анкету.", show_alert=True)
        return
    chat_id = callback.message.chat.id
    session = _newbie_sessions.get(user_id)
    data = session["data"] if session and session["chat_id"] == chat_id else storage.get_newbie_draft(chat_id, user_id)
    logging.info("[NEWBIE] Данные анкеты перед принятием: %s", data)
    if not data or not data.get("game_nickname"):
        await callback.answer("Анкета устарела. Запусти /newbie ещё раз.", show_alert=True)
        return
    await callback.answer("Сохраняю…")

    added_by_username = _normalize_username(callback.from_user.username)
    added_by_display_name = callback.from_user.full_name or callback.from_user.first_name or ""
    try:
        ok = storage.add_recruit(
            chat_id,
            data["game_nickname"],
            data["level"],
            data["class_name"],
            data["teamspeak"],
            data["telegram"],
            user_id,
            added_by_username or None,
            added_by_display_name,
        )
        logging.info("[NEWBIE] storage.add_recruit result=%s", ok)
        logging.info("[NEWBIE] Локальная база: записей=%s", len(storage.all_recruits()))
        if not ok:
            await callback.answer("Такой игрок уже есть", show_alert=True)
            await _close_newbie_session(callback, None)
            return

        # Сразу закрываем сессию в памяти и удаляем черновик.
        # После этого обычные сообщения пользователя уже не считаются частью /newbie.
        _newbie_sessions.pop(user_id, None)
        storage.delete_newbie_draft(chat_id, user_id)

        logging.info("[NEWBIE] Новичок сохранён локально в SQLite.")

        # Google Sheets — дополнительное хранилище.
        # Ошибка Google не отменяет локальное сохранение.
        google_date = datetime.now().strftime("%d.%m.%Y %H:%M")
        google_ok = await asyncio.to_thread(
            send_newbie_to_google,
            date=google_date,
            game_nickname=data["game_nickname"],
            level=data["level"],
            class_name=data["class_name"],
            teamspeak=data["teamspeak"],
            telegram=data["telegram"],
            added_by=f"@{added_by_username}" if added_by_username else added_by_display_name,
        )
        if google_ok:
            logging.info("[NEWBIE] Google Sheets: запись добавлена.")
        else:
            logging.warning(
                "[NEWBIE] Google Sheets: запись не отправлена. "
                "SQLite продолжает работать как основная БД."
            )

        # Убираем все сообщения текущей анкеты и все напоминания.
        await _close_newbie_session(callback, None)
        if google_ok:
            status = "SQLite + Google Sheets"
        else:
            status = "SQLite (Google временно недоступен)"
        await callback.message.answer(
            f"✅ Новичок {data['game_nickname']} записан в базу.\n"
            f"☁️ Хранилище: {status}"
        )
    except Exception as exc:
        logging.exception("[NEWBIE] Failed to finalize newbie acceptance")
        _newbie_sessions.pop(user_id, None)
        storage.delete_newbie_draft(chat_id, user_id)
        storage.delete_newbie_message_ids(chat_id, user_id)
        storage.delete_newbie_messages(chat_id, user_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(f"❌ Не удалось записать новичка в локальную базу: {exc}")


def _parse_analytics_command(text: str):
    match = re.match(r"^/analytics(?:@\\w+)?(?:\\s+(.+))?\\s*$", text or "", re.I)
    if not match:
        return None
    target = (match.group(1) or "all").strip()
    return target or "all"


@dp.message(
    F.text,
    lambda message: _parse_analytics_command((message.text or "").strip()) is not None,
)
async def command_analytics(message: Message):
    if not await _is_admin_user(message):
        return
    target = _parse_analytics_command(message.text or "")
    if target is None:
        return

    if target.lower() in {"all", "все", "всё"}:
        total, by_adder = storage.recruit_analytics_all(message.chat.id)
        if not total:
            await message.answer("📊 Аналитика набора пока пустая.")
            return

        lines = [
            "📊 <b>Аналитика набора</b>",
            "",
            f"👥 Всего принято: <b>{total}</b>",
            "",
            "👤 <b>По игрокам, которые принимали:</b>",
        ]
        for username, display_name, count in by_adder:
            who = f"@{username}" if username else (display_name or "неизвестно")
            lines.append(f"• {who} — <b>{count}</b>")

        await message.answer("\\n".join(lines), parse_mode="HTML")
        return

    target = target.lstrip("@").strip()
    rows = storage.recruit_analytics_user(message.chat.id, target)
    if not rows:
        await message.answer(f"📊 У @{target} пока нет принятых через /newbie игроков.")
        return

    lines = [
        "📊 <b>Аналитика набора</b>",
        "",
        f"👤 Участник: <b>@{target}</b>",
        f"👥 Принято: <b>{len(rows)}</b>",
        "",
        "📋 <b>Список:</b>",
    ]
    for index, (nickname, level, class_name, teamspeak, telegram, created_at) in enumerate(rows, 1):
        lines.append(
            f"{index}. {nickname} — {level or '—'}, {class_name or '—'} "
            f"({created_at[:10]})"
        )

    await message.answer("\\n".join(lines), parse_mode="HTML")


@dp.message(
    F.text,
    lambda message: _is_allowed_chat(message)
    and _addressed_to_alina((message.text or "").strip())
    and _parse_recruiter_query((message.text or "").strip()) is not None,
)
async def command_recruiter_list(message: Message):
    if not await _is_admin_user(message):
        return
    recruiter = _parse_recruiter_query(message.text or "")
    if not recruiter:
        return

    if recruiter == "__SELF__":
        username = _normalize_username(message.from_user.username if message.from_user else None)
        user_id = message.from_user.id if message.from_user else None
        rows = storage.recruits_by_adder_user_id(message.chat.id, user_id) if user_id else []
        display = f"@{username}" if username else (message.from_user.full_name if message.from_user else "ты")
    else:
        rows = storage.recruits_by_adder(message.chat.id, recruiter)
        display = f"@{recruiter}"

    if not rows:
        await message.answer(f"📋 У {display} пока нет записанных через /newbie игроков.")
        return

    header = f"📋 Игроки, которых принял {display}:\n\n"
    blocks = []
    for index, (nickname, level, class_name, teamspeak, telegram, created_at) in enumerate(rows, 1):
        date_text = created_at[:10]
        blocks.append(
            f"{index}. 🎮 {nickname}\n"
            f"   ⭐ {level or '—'} | ⚔️ {class_name or '—'}\n"
            f"   🎧 TS: {teamspeak or '—'} | 📱 TG: {telegram or '—'}\n"
            f"   📅 {date_text}"
        )

    chunks = []
    current = header
    for block in blocks:
        candidate = current + block + "\n\n"
        if len(candidate) > 3800 and current != header:
            chunks.append(current.rstrip())
            current = block + "\n\n"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.rstrip())

    for chunk in chunks:
        await message.answer(chunk)


@dp.message(F.text.startswith('/remember'))
async def command_remember(message: Message):
    if not await _is_admin_user(message): return
    memory = re.sub(r'^/remember(?:@\\w+)?\\s*', '', message.text or '', flags=re.I).strip()
    if not memory:
        await message.answer('Формат: /remember событие, мем или важный факт клана')
        return
    storage.add_clan_memory(message.chat.id, memory)
    await message.answer('🧠 Запомнила. Это теперь часть клановой памяти.')


@dp.message(F.text.startswith('/memories'))
async def command_memories(message: Message):
    if not await _is_admin_user(message): return
    rows = storage.clan_memories(message.chat.id, 10)
    if not rows:
        await message.answer('🧠 Клановая память пока пустая.')
        return
    lines = ['🧠 Последние записи клановой памяти:']
    for memory_id, memory, _ in rows:
        lines.append(f'#{memory_id} — {memory}')
    await message.answer('\n'.join(lines))


@dp.message(Command("roulette"))
async def command_roulette(message: Message):
    """Открывает персональную рулетку по обычной команде /roulette."""
    if message.chat.type != "private" or not message.from_user:
        return

    user_id = message.from_user.id
    try:
        member = await message.bot.get_chat_member(settings.group_chat_id, user_id)
    except Exception:
        await message.answer("🎡 Не смогла проверить твоё участие в клане. Попробуй ещё раз.")
        return

    if member.status not in {"member", "administrator", "creator"}:
        await message.answer("🎡 Рулетка Инстинкта доступна только участникам клана.")
        return

    remaining = cooldown_remaining(settings.db_path, settings.group_chat_id, user_id)
    if remaining > 0:
        hours, rem = divmod(remaining, 3600)
        minutes, seconds = divmod(rem, 60)
        await message.answer(
            "🎡 <b>Рулетка уже использована.</b>\n\n"
            f"⏳ Следующее вращение через <b>{hours:02d}:{minutes:02d}:{seconds:02d}</b>.\n\n"
            "Обновление страницы не сбрасывает таймер — ограничение хранится на сервере. 😏",
            parse_mode="HTML",
        )
        return

    token = get_pending(settings.db_path, settings.group_chat_id, user_id)
    if not token:
        token = make_token(message.bot.token, user_id, choose_prize_id())
        save_pending(settings.db_path, settings.group_chat_id, user_id, token)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎡 ОТКРЫТЬ РУЛЕТКУ", web_app=WebAppInfo(url=f"{ROULETTE_WEBAPP_URL}?token={token}"))
    ]])
    await message.answer(
        "🎡 <b>РУЛЕТКА ИНСТИНКТА</b>\n\n"
        "Тебе доступно <b>одно вращение каждые 24 часа</b>.\n"
        "Открывай рулетку и узнай, что решила судьба. 👀",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.message(F.web_app_data)
async def roulette_webapp_result(message: Message):
    if not message.from_user or message.chat.type != "private":
        return
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        await message.answer("🎡 Не удалось прочитать результат рулетки. Попробуй открыть её ещё раз.")
        return

    data_type = data.get("type")
    if data_type not in {"roulette_spin", "roulette_claim"}:
        return

    user_id = message.from_user.id
    token = str(data.get("token") or "")
    prize = verify_token(message.bot.token, token, user_id)
    if prize is None:
        await message.answer("🎡 Результат рулетки недействителен или уже устарел.")
        return

    if data_type == "roulette_claim":
        claimed = claim_spin(settings.db_path, settings.group_chat_id, user_id, prize)
        if not claimed:
            await message.answer("🎁 Этот приз уже был получен или опубликован. Повторно получить его нельзя. 😏")
            return

        display_name = message.from_user.full_name or message.from_user.first_name or "Игрок"
        special_group = {
            "empty_gift": f"🎁 <b>АЛИНА СЧИТАЕТ, ЧТО ЭТО ПОДАРОК</b>\n\n<b>{escape(display_name)}</b> получил(а) <b>ПУСТОЙ ПОДАРОК</b>.\n\nВнутри ничего нет. Алина говорит, что он очень редкий. 😂",
            "golden_air": f"💨 <b>ЗОЛОТОЙ ВОЗДУХ!</b>\n\n<b>{escape(display_name)}</b> получил(а) золотой воздух.\n\nДышать можно как обычно. Но он золотой. ✨",
            "good_person_certificate": f"📜 <b>ОФИЦИАЛЬНАЯ СПРАВКА АЛИНЫ</b>\n\n<b>{escape(display_name)}</b> сегодня молодец. Оснований нет. Справка есть. 😌",
            "alina_question_coupon": f"🎟️ <b>КУПОН ОТ АЛИНЫ</b>\n\n<b>{escape(display_name)}</b> получил(а) один бесплатный вопрос Алине. Да, спрашивать можно было и без купона. Но теперь это официально. 😂",
            "legendary_potato": f"🥔 <b>ЛЕГЕНДАРНАЯ КАРТОШКА!</b>\n\n<b>{escape(display_name)}</b> получил(а) легендарный артефакт неизвестного назначения. Алина уверяет, что это очень важно.",
            "lucky_sock": f"🧦 <b>НОСОК АБСОЛЮТНОЙ УДАЧИ!</b>\n\n<b>{escape(display_name)}</b> получил(а) носок. Второго носка не существует. В этом и заключается его сила.",
            "useless_power_crystal": f"💎 <b>КРИСТАЛЛ БЕСПОЛЕЗНОЙ МОЩИ!</b>\n\n<b>{escape(display_name)}</b> получил(а) +999 к понтам и +0 к характеристикам.",
            "busy_wand": f"🪄 <b>ПАЛОЧКА «СДЕЛАЙ ВИД, ЧТО Я ЗАНЯТ»</b>\n\n<b>{escape(display_name)}</b> теперь может убедительно выглядеть занятым, ничего не делая.",
            "audacity_license": f"🪪 <b>ЛИЦЕНЗИЯ НА НАГЛОСТЬ</b>\n\n<b>{escape(display_name)}</b> официально разрешено быть наглым 24 часа. Алина постарается не осуждать. 😈",
            "alina_eye": f"👀 <b>ОКО АЛИНЫ</b>\n\n<b>{escape(display_name)}</b> теперь под наблюдением Алины. Скрыться невозможно. 😈",
            "dragon_scale": f"🐉 <b>ЧЕШУЯ ДРАКОНА!</b>\n\n<b>{escape(display_name)}</b> получил(а) защиту от критики, здравого смысла и сомнительных решений.",
            "favorite_crown": f"👑 <b>ГЛАВНЫЙ ЛЮБИМЧИК АЛИНЫ!</b>\n\n<b>{escape(display_name)}</b> становится главным любимчиком Алины на 24 часа. Остальным разрешается завидовать. 😌",
            "alina_brain": f"🧠 <b>ЛЕГЕНДАРНЫЙ ВЫИГРЫШ!</b>\n\n<b>{escape(display_name)}</b> получил(а) <b>МОЗГ АЛИНЫ НА ПРОКАТ</b> на 24 часа.\n\nАлина: «Верни мне его. Мне ещё работать». 😂",
        }
        group_text = special_group.get(
            prize.id,
            f"🎡 <b>РЕЗУЛЬТАТ РУЛЕТКИ ИНСТИНКТА</b>\n\n👤 Победитель: <b>{escape(display_name)}</b>\n\n{prize.emoji} <b>{escape(prize.title)}</b>\n\n{prize.description}",
        )
        await message.bot.send_message(
            settings.group_chat_id,
            group_text,
            message_thread_id=2,
            parse_mode="HTML",
        )
        await message.answer("🎉 Алина уже поздравила тебя в основном чате клана!")
        return

    if spun_today(settings.db_path, settings.group_chat_id, user_id):
        await message.answer("🎡 Это вращение уже использовано. Следующее будет доступно через 24 часа. 😏")
        return

    saved = save_spin(
        settings.db_path,
        settings.group_chat_id,
        user_id,
        message.from_user.username,
        message.from_user.full_name or message.from_user.first_name,
        prize,
    )
    if not saved:
        await message.answer("🎡 Это вращение уже было засчитано. Следующее будет доступно через 24 часа.")
        return

    await message.answer(
        f"🎉 <b>ТЕБЕ ВЫПАЛО: {prize.emoji} {prize.title.upper()}!</b>\n\n"
        f"{prize.description}\n\n"
        "Нажми «ПОЛУЧИТЬ ПРИЗ», чтобы Алина поздравила тебя в основном чате клана.",
        parse_mode="HTML",
    )


@dp.message(Command('start'))
async def command_start(message: Message):
    # Ежедневная кнопка рулетки может открыть персональный запуск сразу.
    if message.chat.type == "private" and message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip().lower() == "roulette":
            await command_roulette(message)
            return
    if not await _is_admin_user(message): return
    storage.set_chat_enabled(message.chat.id, True)
    await message.answer('🟢 Бот запущен. Теперь отвечаю на сообщения.')


@dp.message(Command('stop'))
async def command_stop(message: Message):
    if not await _is_admin_user(message): return
    storage.set_chat_enabled(message.chat.id, False)
    await message.answer('🔴 Бот остановлен. Команду /start можно использовать для запуска.')


@dp.message(Command('status'))
async def command_status(message: Message):
    if not await _is_admin_user(message): return
    await message.answer(f"Статус бота: {'🟢 запущен' if storage.is_chat_enabled(message.chat.id) else '🔴 остановлен'}.")


@dp.callback_query(F.data == "yt_test_like")
async def callback_yt_test_like(callback: CallbackQuery):
    if callback.message is None or not _is_allowed_chat(callback.message):
        await callback.answer()
        return

    if not await _is_admin_callback(callback):
        await callback.answer("Эта кнопка доступна только администраторам.", show_alert=True)
        return

    await callback.answer("Запускаю проверку YouTube…")

    try:
        latest = await asyncio.to_thread(_latest_video)
        if not latest:
            await callback.message.answer("Не смогла найти последнее видео @k4mui_play.")
            return

        video_id, title, _url, _published = latest
        ok = await asyncio.to_thread(like_video, video_id)

        if ok:
            text = (
                f"🧪 Тест завершён.\n\n"
                f"Видео: «{title}»\n"
                f"👍 Лайк поставлен или уже был установлен ранее.\n"
                f"Аккаунт: linaabildina@gmail.com"
            )
        else:
            text = (
                f"🧪 Тест не пройден.\n\n"
                f"Видео: «{title}»\n"
                f"👍 Лайк не поставлен.\n"
                f"Проверь открывшийся Chromium: активным должен быть аккаунт linaabildina@gmail.com."
            )

        await callback.message.answer(text)
    except Exception:
        logging.exception("YouTube like test failed")
        await callback.message.answer(
            "🧪 Тест не пройден. Не удалось выполнить действие в YouTube. "
            "Проверь Playwright/Chromium и авторизацию аккаунта linaabildina@gmail.com."
        )


@dp.message(Command("roulette_stats"))
async def command_roulette_stats(message: Message):
    if not await _is_admin_user(message):
        return
    total, rows = roulette_stats(settings.db_path, message.chat.id)
    lines = [
        "🎡 <b>СТАТИСТИКА РУЛЕТКИ INSTINCT</b>",
        "",
        f"Всего вращений: <b>{total}</b>",
    ]
    if rows:
        lines.append("")
        lines.extend(
            f"{i}. {title} — <b>{count}</b>"
            for i, (_pid, title, count) in enumerate(rows, 1)
        )
    else:
        lines.append("\nПока никто не крутил рулетку.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text.startswith('/watch'))
async def command_watch(message: Message):
    if not await _is_admin_user(message): return
    item, max_price = _parse_watch_command(message.text or '')
    if not item:
        await message.answer('Формат: /watch предмет [до 4 000 000]')
        return
    watch_id = add_watch(settings.db_path, message.chat.id, message.from_user.id if message.from_user else None, item, max_price)
    limit = f" до {max_price:,}".replace(',', ' ') if max_price else ''
    await message.answer(f'🔔 Поставила на контроль №{watch_id}: {item}{limit}\nПроверяю котобазу каждые 15 минут.')


@dp.message(F.text.startswith('/watches'))
async def command_watches(message: Message):
    if not await _is_admin_user(message): return
    rows = list_watches(settings.db_path, message.chat.id)
    if not rows:
        await message.answer('Активных наблюдений нет.')
        return
    lines = ['🔔 Наблюдения:']
    for watch_id, item, max_price in rows:
        limit = f" ≤ {max_price:,}".replace(',', ' ') if max_price else ''
        lines.append(f'#{watch_id} — {item}{limit}')
    lines.append('\nУдалить: /unwatch ID')
    await message.answer('\n'.join(lines))


@dp.message(F.text.startswith('/unwatch'))
async def command_unwatch(message: Message):
    if not await _is_admin_user(message): return
    match = re.search(r'^/unwatch(?:@\w+)?\s+(\d+)', message.text or '', re.I)
    if not match:
        await message.answer('Формат: /unwatch ID')
        return
    ok = remove_watch(settings.db_path, message.chat.id, int(match.group(1)))
    await message.answer('🗑 Наблюдение удалено.' if ok else 'Не нашла такое наблюдение.')


@dp.message(F.text.startswith('/history'))
async def command_history(message: Message):
    if not await _is_admin_user(message): return
    item = re.sub(r'^/history(?:@\\w+)?\\s*', '', message.text or '', flags=re.I).strip()
    if not item:
        await message.answer('Формат: /history предмет')
        return
    from app.game_features import price_analysis
    await message.answer(price_analysis(settings.db_path, message.chat.id, item))


@dp.message(F.text.startswith('/market'))
async def command_market(message: Message):
    if not await _is_admin_user(message): return
    from app.game_features import market_summary
    await message.answer(market_summary(settings.db_path, message.chat.id))


@dp.message(F.new_chat_members)
async def on_new_chat_members(message: Message):
    """Автоматически приветствует новых участников группы."""
    if not _is_allowed_chat(message):
        return

    new_members = [member for member in (message.new_chat_members or []) if not (_bot_id and member.id == _bot_id)]
    if not new_members:
        return

    for member in new_members:
        try:
            storage.record_clan_join(
                settings.group_chat_id,
                member.id,
                member.username,
                member.full_name or member.first_name or "Новый игрок",
            )
        except Exception:
            logging.exception("Could not record clan join event")

    greetings = [
        "Добро пожаловать, {name}! 👋 Осваивайся, у нас тут весело 😏",
        "О, новенький! {name}, добро пожаловать в клан 👀",
        "Встречаем {name}! 👋 Заходи, располагайся.",
        "{name}, добро пожаловать! 😌 Теперь ты официально с нами.",
        "Так-так, к нам прибыло подкрепление — {name}! 🔥 Добро пожаловать!",
    ]

    names = [member.full_name or member.first_name or "новенький" for member in new_members]
    if len(names) == 1:
        text = random.choice(greetings).format(name=names[0])
    else:
        text = "Добро пожаловать в клан! 👋\\n\\n" + "\\n".join(f"• {name}" for name in names)
        text += "\\n\\nОсваивайтесь, теперь вы с нами 😏"

    await message.bot.send_message(
        settings.group_chat_id,
        text,
        message_thread_id=2,
    )


def _is_general_greeting(text: str) -> bool:
    normalized = re.sub(r"[^\w\s@-]", " ", (text or "").lower(), flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or len(normalized) > 80:
        return False
    if re.search(r"@\w+", normalized):
        return False
    greeting_patterns = (
        r"^(привет|прив|здарова|здорово|доброе утро|добрый день|добрый вечер|доброй ночи|"
        r"доброе утречко|добрый вечерок|всем привет|всем доброе утро|доброе утро всем)"
        r"(?: всем| народ| клан)?$",
    )
    return any(re.fullmatch(pattern, normalized) for pattern in greeting_patterns)


@dp.message(F.text)
async def on_message(message: Message):
    global _bot_id, _bot_username
    if not _is_allowed_chat(message): return
    original_text = (message.text or '').strip()
    if not original_text: return
    if _bot_id is not None and message.from_user and message.from_user.id == _bot_id: return
    if original_text.split()[0].split('@')[0].lower() in {'/start','/stop','/status','/consultant','/roulette','/watch','/watches','/unwatch','/history','/market','/reminders','/cancel','/newbie','/roles','/officer','/unofficer','/officers'}: return
    if not storage.is_chat_enabled(message.chat.id): return
    is_reply_to_alina = bool(message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == _bot_id)
    is_mention_to_alina = _mentioned_alina(message)
    reply_context = ""
    if message.reply_to_message and message.reply_to_message.text:
        reply_context = message.reply_to_message.text.strip()
    display_name = message.from_user.full_name if message.from_user else None
    user_id = message.from_user.id if message.from_user else None
    previous_seen = storage.user_last_seen(message.chat.id, user_id) if user_id else None
    storage.remember_user(message.chat.id, user_id, message.from_user.username if message.from_user else None, display_name, original_text)

    # Алина замечает возвращение участника после долгого отсутствия.
    if previous_seen and user_id:
        try:
            last_seen_dt = datetime.fromisoformat(previous_seen)
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=__import__('datetime').timezone.utc)
            hours_away = (datetime.now(__import__('datetime').timezone.utc) - last_seen_dt).total_seconds() / 3600
            if hours_away >= 24:
                return_name = display_name or (f'@{message.from_user.username}' if message.from_user and message.from_user.username else 'ты')
                return_messages = [
                    f'О, {return_name} воскресла 😏 Я уже думала, куда ты пропала.',
                    f'О, {return_name} вернулась 👀 А я уже заметила, что тебя давно не было.',
                    f'Наконец-то {return_name} объявилась 😌 Я тебя уже потеряла.',
                    f'О, {return_name} снова с нами 😏 Где пропадала?',
                ]
                await message.answer(random.choice(return_messages))
        except (ValueError, TypeError, OverflowError):
            pass

    recruiter = _parse_recruiter_query(original_text)
    if recruiter and _is_allowed_chat(message) and _addressed_to_alina(original_text):
        await command_recruiter_list(message)
        return

    if not _addressed_to_alina(original_text) and not is_reply_to_alina and not is_mention_to_alina: return

    # Если игрок отвечает на чужое сообщение и в Reply упоминает @instinctbot_bot,
    # Алина должна понять, что обращаются к ней, и использовать исходное сообщение как контекст.
    text = original_text
    if is_mention_to_alina and reply_context and not is_reply_to_alina:
        text = f"Контекст сообщения, на которое ответили:\n{reply_context}\n\nСообщение пользователя:\n{original_text}"

    # Вопросы о YouTube-канале обрабатываем отдельно: Алина может
    # проверить реальный статус лайка авторизованного аккаунта.
    if _is_youtube_question(text):
        try:
            answer = await _youtube_status_answer()
        except Exception:
            logging.exception("YouTube status check failed")
            answer = "Не смогла проверить статус лайка на YouTube. Авторизация аккаунта ещё не подключена или доступ временно недоступен."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🧪 Тест: поставить лайк", callback_data="yt_test_like")
        ]])
        await message.answer(answer, reply_to_message_id=message.message_id, reply_markup=keyboard)
        storage.add(message.chat.id, None, None, 'assistant', answer)
        return

    # Если участник явно исправляет Алину, сохраняем это как приоритетную
    # корректировку знаний. Специальная команда не нужна.
    if _is_knowledge_correction(text):
        storage.add_knowledge_correction(message.chat.id, text)
        logging.info("[KNOWLEDGE] Saved user correction: %s", text[:300])

    # Иногда Алина реагирует на сообщение прямо в Telegram, без отдельного текста.
    # Это делает реакцию естественной и не превращает каждый ответ в спам реакциями.
    if random.random() < 0.35:
        try:
            await message.react(reaction=[ReactionTypeEmoji(emoji=random.choice(['👍', '❤️', '😂', '🔥', '👀']))])
        except Exception as exc:
            logging.debug('Could not add Telegram reaction: %s', exc)

    storage.add(message.chat.id, message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, 'user', text)
    try:
        answer = _strip_urls(await ai.decide_and_answer(message.chat.id, text))
        if not answer: return
        await message.answer(answer, reply_to_message_id=message.message_id)
        storage.add(message.chat.id, None, None, 'assistant', answer)
        if _is_general_greeting(original_text):
            try:
                from zoneinfo import ZoneInfo
                greeting_now = datetime.now(ZoneInfo("Asia/Almaty"))
                if greeting_now.hour < 10:
                    storage.mark_morning_greeting(message.chat.id, greeting_now.date().isoformat())
            except Exception:
                logging.exception("Could not mark morning greeting")

    except Exception as exc:
        logging.exception('Failed to generate/send answer: %s', exc)




async def _reminders_forever(bot: Bot):
    loop = asyncio.get_running_loop()

    def send_message(chat_id, text):
        future = asyncio.run_coroutine_threadsafe(bot.send_message(chat_id, text), loop)
        future.result(timeout=30)

    while True:
        try:
            await asyncio.to_thread(reminder_service.send_due, send_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception('Reminder delivery failed: %s', exc)
        await asyncio.sleep(5)


async def _morning_greeting_forever(bot: Bot):
    """Каждое утро около 10:00 Алина сама здоровается с кланом."""
    while True:
        now = datetime.now().astimezone()
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= target:
            target += __import__('datetime').timedelta(days=1)
        await asyncio.sleep(max(1, (target - datetime.now().astimezone()).total_seconds()))
        try:
            from zoneinfo import ZoneInfo
            greeting_now = datetime.now(ZoneInfo("Asia/Almaty"))
            greeting_date = greeting_now.date().isoformat()
            if storage.morning_greeting_done(settings.group_chat_id, greeting_date):
                continue

            greetings = [
                'Всем привет! ☀️',
                'Всем доброе утро 😊',
                'Доброе утро, народ! 👋',
                'Всем привет! Ну что, просыпаемся? 😌',
                'Доброе утро, клан 🌞',
            ]
            await bot.send_message(settings.group_chat_id, random.choice(greetings), message_thread_id=2)
            storage.mark_morning_greeting(settings.group_chat_id, greeting_date)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception('Morning greeting failed: %s', exc)




async def _roulette_announcement_forever(bot: Bot):
    """Каждый день в 22:00 по времени Алматы смешно предлагает клану прокрутить рулетку."""
    from zoneinfo import ZoneInfo
    while True:
        now = datetime.now(ZoneInfo("Asia/Almaty"))
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1, (target - now).total_seconds()))
        try:
            me = await bot.get_me()
            if not me.username:
                continue
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎰 КРУТИТЬ РУЛЕТКУ",
                    url=f"https://t.me/{me.username}?start=roulette",
                )
            ]])
            await bot.send_message(
                settings.group_chat_id,
                "🎰 <b>Так, народ…</b>\n\n"
                "Кто сегодня ещё не испытал судьбу? 😈\n\n"
                "В рулетке вас ждут легендарная картошка, золотой воздух, "
                "мозг Алины и прочие жизненно необходимые вещи. 😂\n\n"
                "<b>Один спин — раз в 24 часа.</b>\n\n"
                "Кто рискнёт? 👀",
                message_thread_id=2,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Roulette announcement failed")

async def _daily_recruit_check_forever(bot: Bot):
    """Раз в сутки проверяет, были ли новые вступления за последние 24 часа."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Almaty")
    while True:
        try:
            now = datetime.now(tz)
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep(max(1, (target - now).total_seconds()))

            check_now = datetime.now(tz)
            check_date = check_now.date().isoformat()
            if not storage.daily_join_check_claim(settings.group_chat_id, check_date):
                continue

            since = check_now - timedelta(hours=24)
            joins = storage.clan_joins_since(settings.group_chat_id, since)
            if joins:
                logging.info("Daily recruit check: %s new clan members.", len(joins))
                continue

            messages = [
                "😏 Капитаны, вы там вообще кого-нибудь вербуете?\nЗа последние сутки — ни одного нового игрока. Клан сам себя расширять, видимо, будет.",
                "👀 Докладываю обстановку: новых игроков за сутки не обнаружено.\nКапитаны, это что — секретная операция по набору невидимок?",
                "📢 Капитаны, у меня плохие новости…\nНовых игроков за сутки — 0. Вы там людей в клан зовёте или просто любуетесь списком участников? 😂",
                "🧐 Проверила приток новичков за сутки.\nПусто. Тишина. Ни одного нового лица.\nКапитаны, пора просыпаться — я одна клан не расширю! 😈",
                "🚨 Срочный отчёт Алины: новых игроков за сутки нет.\nКапитаны, похоже, отдел набора ушёл в отпуск без предупреждения. 😂",
            ]
            await bot.send_message(
                settings.group_chat_id,
                random.choice(messages),
                message_thread_id=2,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Daily recruit check failed")
            await asyncio.sleep(60)



async def _youtube_forever(bot: Bot):
    loop = asyncio.get_running_loop()

    def send_message(text):
        future = asyncio.run_coroutine_threadsafe(
            bot.send_message(settings.group_chat_id, text, message_thread_id=2), loop
        )
        future.result(timeout=30)

    await asyncio.to_thread(monitor_forever, settings.db_path, send_message, like_video)


async def _sync_forum_forever():
    while True:
        try:
            sources = [x.strip() for x in settings.knowledge_sources.splitlines() if x.strip()]
            if sources: await asyncio.to_thread(collect_sources, sources, 20000)
        except asyncio.CancelledError: raise
        except Exception as exc: logging.exception('Knowledge synchronization failed: %s', exc)
        await asyncio.sleep(settings.knowledge_refresh_minutes * 60)


async def _watch_forever(bot: Bot):
    loop = asyncio.get_running_loop()
    def send_message(chat_id, text):
        future = asyncio.run_coroutine_threadsafe(bot.send_message(chat_id, text), loop)
        future.result(timeout=30)
    while True:
        try:
            await asyncio.to_thread(check_watches, settings.db_path, send_message)
        except asyncio.CancelledError: raise
        except Exception as exc: logging.exception('Watch synchronization failed: %s', exc)
        await asyncio.sleep(15 * 60)


def request_stop():
    global _polling_loop, _polling_task
    if _polling_loop and _polling_task and not _polling_task.done(): _polling_loop.call_soon_threadsafe(_polling_task.cancel)


async def main():
    global _bot_id, _polling_loop, _polling_task, _knowledge_sync_task, _news_monitor_thread, _watch_task, _reminder_task, _youtube_task, _morning_greeting_task, _roulette_task, _daily_recruit_check_task
    _polling_loop = asyncio.get_running_loop()
    bot = Bot(settings.telegram_token)
    me = await bot.get_me()
    _bot_id = me.id
    _bot_username = me.username
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await bot.set_my_commands([], scope=BotCommandScopeDefault())
        await _sync_role_command_menus(bot)
    except Exception:
        logging.exception("Failed to configure role-based command menus")
    _knowledge_sync_task = asyncio.create_task(_sync_forum_forever())
    _watch_task = asyncio.create_task(_watch_forever(bot))
    _reminder_task = asyncio.create_task(_reminders_forever(bot))
    _youtube_task = asyncio.create_task(_youtube_forever(bot))
    _morning_greeting_task = asyncio.create_task(_morning_greeting_forever(bot))
    _roulette_task = asyncio.create_task(_roulette_announcement_forever(bot))
    _daily_recruit_check_task = asyncio.create_task(_daily_recruit_check_forever(bot))
    if run_news_monitor_in_thread is not None:
        import threading
        _news_monitor_thread = threading.Thread(target=run_news_monitor_in_thread, name='telegram-news-monitor', daemon=True)
        _news_monitor_thread.start()
    try:
        _polling_task = asyncio.current_task()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in (_knowledge_sync_task, _watch_task, _reminder_task, _youtube_task, _morning_greeting_task, _roulette_task, _daily_recruit_check_task):
            if task and not task.done(): task.cancel()
        await bot.session.close()


if __name__ == '__main__': asyncio.run(main())