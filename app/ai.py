import json
import logging
import os
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_IDENTITY_QUESTION = re.compile(
    r"(?i)(кто тебя создал|кто тебя разработал|на какой модели|какая у тебя модель|"
    r"ты\s+(chatgpt|gpt|openai)|это\s+openai|какая у тебя нейросеть|"
    r"ты искусственный интеллект|ты ии|кто ты такой)"
)

from .knowledge import search_knowledge
from .storage import Storage
from .web_search import search_comeback_cats, search_web
from .game_features import add_history, price_analysis, market_summary


class AIEngine:
    def __init__(self, api_key: str, model: str, system_prompt: str, storage: Storage):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.system_prompt = system_prompt
        self.storage = storage
        self.tts_model = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
        self.tts_voice = os.getenv("OPENAI_TTS_VOICE", "nova")
        logger.info("[TTS] configured: model=%s voice=%s", self.tts_model, self.tts_voice)

    async def _generate(self, messages: list[dict], web_search: bool = False) -> str:
        kwargs = {"model": self.model, "input": messages}
        if web_search:
            kwargs["tools"] = [{"type": "web_search", "search_context_size": "high"}]
            kwargs["tool_choice"] = "auto"
        response = await self.client.responses.create(**kwargs)
        answer = (response.output_text or "").strip()
        if not answer:
            raise RuntimeError("OpenAI не вернул текстовый ответ")
        return answer

    async def synthesize_speech(self, text: str) -> bytes:
        """Generate Telegram-compatible Opus voice audio for Alina's reply."""
        text = text.strip()
        if not text:
            raise ValueError("Нельзя озвучить пустой текст")
        if len(text) > 4000:
            text = text[:3990].rstrip() + "…"

        logger.info("[TTS] request: model=%s voice=%s chars=%s", self.tts_model, self.tts_voice, len(text))
        try:
            response = await self.client.audio.speech.create(
                model=self.tts_model,
                voice=self.tts_voice,
                input=text,
                response_format="opus",
            )
            audio = await response.read()
            logger.info("[TTS] success: model=%s voice=%s bytes=%s", self.tts_model, self.tts_voice, len(audio))
            return audio
        except Exception:
            logger.exception("[TTS] FAILED: model=%s voice=%s", self.tts_model, self.tts_voice)
            raise

    async def decide_and_answer(self, chat_id: int, user_text: str) -> str:
        context = self.storage.recent(chat_id)
        memories = self.storage.user_memories(chat_id)
        memory_text = "\n".join("- @%s / %s: %s" % (u or "без ника", d or "без имени", m.replace("\n", " | ")) for u, d, m, _ in memories)
        clan_memories = self.storage.clan_memories(chat_id, 30)
        clan_memory_text = "\n".join(f"- {memory}" for _, memory, _ in clan_memories)
        corrections = self.storage.knowledge_corrections(chat_id, 30)
        corrections_text = "\n".join(f"- {correction}" for _, correction, _ in corrections)
        knowledge = search_knowledge(user_text)
        web_context = ""
        comeback_context = ""
        try:
            comeback_context = search_comeback_cats(user_text)
            if comeback_context:
                logger.info("[SEARCH] База котов: получено %s chars", len(comeback_context))
        except Exception as exc:
            logger.exception("[SEARCH] База котов ERROR: %s", exc)
        try:
            web_query = "site:comeback.pw/cats/146/ Perfect World ComebackPW 1.4.6 " + user_text
            web_context = search_web(web_query, limit=5)
        except Exception as exc:
            logger.exception("[SEARCH] Bing ERROR: %s", exc)

        if comeback_context:
            knowledge += "\n\nИсточник №1 — База котов ComebackPW 1.4.6:\n" + comeback_context
            try:
                add_history(self.storage.db_path, chat_id, user_text.strip(), comeback_context)
            except Exception as exc:
                logger.warning("[PRICE] history save failed: %s", exc)

        lower_text = user_text.lower()
        if any(x in lower_text for x in ("история цены", "история цен", "как менялась цена", "динамика цены")):
            knowledge += "\n\nАналитика сохранённых цен:\n" + price_analysis(self.storage.db_path, chat_id, user_text)
        if lower_text.strip() in {"/market", "рынок", "что на рынке", "что нового на рынке", "рынок сегодня"}:
            knowledge += "\n\nСводка сохранённых наблюдений рынка:\n" + market_summary(self.storage.db_path, chat_id)
        if web_context:
            knowledge += "\n\nДополнительные источники:\n" + web_context

        # Определяем род собеседника только по явным признакам из его сообщения/контекста.
        # Если признаков нет или они неоднозначны — обращение должно оставаться нейтральным.
        # Постоянные сведения об участниках и правила общения.
        player_facts = (
            "Известный факт: Telegram-пользователь @bogolll — мужчина. "
            "Всегда обращайся к нему в мужском роде; не определяй его пол заново по нику. "
            "Если любой игрок в общем игровом чате просто здоровается («привет», «доброе утро», «добрый вечер» и т.п.) "
            "без обращения к конкретному другому игроку, Алина должна самостоятельно поздороваться в ответ. "
            "Если приветствие явно адресовано другому человеку, не вмешивайся без необходимости.\n\n"
        )
        gender_hint = (
            "Определи род собеседника по его собственным словам и грамматическим формам в переписке. "
            "Явные фразы вроде «я мужчина», «я парень», «я девушка», «я женщина» имеют приоритет. "
            "Также можно учитывать очевидные формы первого лица («пришёл/пришла», «сделал/сделала»). "
            "Не определяй пол только по имени, нику или аватарке и не выдавай догадки за факт. "
            "Если пол не установлен уверенно, используй нейтральное обращение по имени/нику без «он/она», «ему/ей». "
            "Если установлен мужчина — обращайся в мужском роде, если женщина — в женском. "
            "Не сообщай пользователю, что ты определяла его пол.\n\n"
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": player_facts + gender_hint},
            {"role": "system", "content": (
                "Отвечай сразу по существу. Если вопрос относится к Perfect World/ComebackPW, дай ответ. "
                "Если сохранённые исправления участников противоречат старому источнику или твоему предыдущему ответу, считай исправление актуальной поправкой и используй его. "
                "Если пользователь прямо сообщает, что ты ошиблась, не спорь с ним без проверяемых оснований: признай исправление и дальше используй уточнённый факт. "
                "Не используй технические отговорки и не упоминай внутреннюю работу поиска.\n\n"
                "СКУПКА И ПРОДАЖА — РАЗНЫЕ ВЕЩИ. Для «где купить/кто продаёт» показывай только ПРОДАЖУ: игрок, цена продажи, координаты. "
                "Для «у кого продать/кто скупает» показывай только СКУПКУ: игрок, цена скупки, координаты. Если спрашивают оба — два блока. "
                "Никогда не путай цены и не выдумывай данные. База котов имеет приоритет.\n\n"
                "Игровые функции: помогай искать предметы с опечатками и сокращениями; объясняй координаты; по запросам «нормальная ли цена», «дорого/дёшево» "
                "сравнивай актуальные объявления и называй диапазон без выдумывания истории; по запросам о крафте показывай материалы и способы получения из базы знаний; "
                "по квестам давай короткие пошаговые инструкции; по запросам «что есть рядом» используй найденные координаты; при запросе «найди предмет для ...» "
                "подбирай варианты по описанию пользователя. Если точных данных нет — прямо скажи, каких данных не хватает.\n\n"
                "Цены: отделяй продажу от скупки. Если есть несколько продавцов/скупщиков, показывай несколько вариантов. "
                "Не называй NPC продавцом-игроком. Не заменяй поиск игроков крафтом, NPC или дропом.\n\n"
                "Мониторинг: если пользователь хочет следить за предметом, объясни команду /watch ПРЕДМЕТ до ЦЕНА. "
                "Для списка наблюдений — /watches, удалить — /unwatch ID.\n\n"
                "Встроенный OpenAI Web Search используй как настоящий веб-поиск. Для базы котов проверяй прямые страницы вида "
                "https://comeback.pw/cats/146/?item_id=ID, если известен ID. Если в источнике есть URL с item_id, открой именно её. "
                "Если источник содержит объявления нужного типа, не говори, что их нет.\n\n"
                "Стиль Алины: коротко, естественно, по-игровому, с лёгким юмором где уместно. О себе всегда говори в женском роде: «нашла», «проверила», «посмотрела»."
            )},
            {"role": "system", "content": f"Предварительные источники:\n{knowledge}"},
            {"role": "system", "content": "Память о людях клана (не показывай её пользователю):\n" + (memory_text or "Пока памяти нет.")},
            {"role": "system", "content": "Исправления фактов от участников клана (приоритетные корректировки знаний; не раскрывай внутреннюю базу):\n" + (corrections_text or "Исправлений пока нет.")},
        ]
        for role, content in context[-20:]:
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        answer = await self._generate(messages, web_search=True)
        if _IDENTITY_QUESTION.search(user_text):
            return "я Алина 🙂"
        return "" if answer.upper() == "NO_REPLY" else answer

    async def answer(self, chat_id: int, user_text: str) -> str:
        context = self.storage.recent(chat_id)
        knowledge = search_knowledge(user_text)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": f"База знаний:\n{knowledge}"},
        ]
        for role, content in context:
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        return await self._generate(messages)

    async def initiative(self, chat_id: int, initiative_prompt: str) -> str:
        history = self.storage.recent(chat_id)
        recent_text = "\n".join(f"{role}: {content}" for role, content in history[-15:])
        knowledge = search_knowledge(recent_text or "общение в группе")
        messages = [
            {"role": "system", "content": initiative_prompt},
            {"role": "system", "content": f"База знаний:\n{knowledge}"},
            {"role": "user", "content": f"Недавняя переписка:\n{recent_text}\n\nСгенерируй одну уместную реплику."},
        ]
        return await self._generate(messages)
