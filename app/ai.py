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

    async def extract_clan_memories(self, chat_id: int, recent_context: list[tuple[str, str]], user_text: str):
        """Автоматически извлекает устойчивые факты клана из текущей переписки."""
        recent_text = '\n'.join(f'{role}: {content}' for role, content in recent_context[-12:])
        prompt = (
            'Ты — модуль памяти игрового Telegram-клана. Анализируй новое сообщение и недавний контекст. '
            'Сохраняй только конкретные подтверждённые факты, полезные в будущем. '
            'Ищи: кто с кем играет; игровые достижения; важные события; внутренние мемы; дружеские подколы; важные договорённости. '
            'Не сохраняй пароли, телефоны, адреса, секреты, обычные реплики, вопросы, догадки или неподтверждённые слухи. '
            'Не придумывай связи между игроками. Верни только JSON-массив. '
            'Поля: category, fact, people, confidence. category: players|achievement|event|meme|trolling|important. '
            'Сохраняй только confidence >= 0.80. Если подходящих фактов нет — верни [].'
        )
        messages = [
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': f'Недавний контекст:\n{recent_text}\n\nНовое сообщение:\n{user_text}'},
        ]
        try:
            raw = (await self._generate(messages)).strip()
            if raw.startswith('```'):
                raw = '\n'.join(raw.splitlines()[1:-1]).strip()
            data = json.loads(raw)
            if not isinstance(data, list):
                return
            for item in data[:5]:
                if not isinstance(item, dict):
                    continue
                category = str(item.get('category') or '').strip().lower()
                fact = str(item.get('fact') or '').strip()
                people = str(item.get('people') or '').strip() or None
                try:
                    confidence = float(item.get('confidence', 0))
                except (TypeError, ValueError):
                    confidence = 0
                if confidence < 0.80 or not fact:
                    continue
                self.storage.add_clan_memory_fact(chat_id, category, fact, people=people, confidence=confidence)
        except Exception:
            logger.debug('[MEMORY] Clan memory extraction skipped', exc_info=True)
    async def extract_player_profile(self, chat_id: int, user_id: int, username: str | None, display_name: str | None, user_text: str, recent_context: list[tuple[str, str]]):
        """Extract only useful, reasonably supported player facts and merge them into long-term memory."""
        context = "\n".join(f"{role}: {content}" for role, content in recent_context[-8:])
        prompt = (
            "Ты — аккуратный модуль долговременной памяти игрока в дружеском игровом клане. "
            "Извлекай только устойчивые сведения, которые сам игрок прямо сообщил или которые явно подтверждены контекстом. "
            "Не делай выводы о поле, возрасте, месте жительства, здоровье, политике, религии или других чувствительных признаках. "
            "Не превращай шутки, сарказм, предположения и чужие слова в факты о пользователе. "
            "Возвращай только JSON-объект с полями interests, personal_facts, communication_style, recurring_topics. "
            "Каждое поле — короткая строка или пустая строка. Не повторяй уже известное; не добавляй общие слова. "
            "interests — игровые/обычные интересы; personal_facts — подтверждённые факты и предпочтения; "
            "communication_style — наблюдаемая манера общения без ярлыков; recurring_topics — повторяющиеся темы. "
            "Если нечего сохранять, верни все поля пустыми."
        )
        try:
            raw = (await self._generate([
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Недавний контекст:\n{context}\n\nНовое сообщение игрока:\n{user_text[:2500]}"},
            ])).strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.splitlines()[1:-1]).strip()
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            values = {
                key: str(data.get(key) or "").strip()[:300]
                for key in ("interests", "personal_facts", "communication_style", "recurring_topics")
            }
            if any(values.values()):
                self.storage.learn_player_profile(
                    chat_id, user_id, username, display_name,
                    interests=values["interests"],
                    personal_facts=values["personal_facts"],
                    communication_style=values["communication_style"],
                    recurring_topics=values["recurring_topics"],
                    source_text=user_text,
                )
        except Exception:
            logger.debug("[MEMORY] Player profile extraction skipped", exc_info=True)

    async def choose_sticker_id(self, user_text: str, draft_answer: str, stickers: list[dict]) -> int | None:
        """Выбирает стикер по смыслу переписки; None означает, что лучше ответить текстом."""
        if not stickers:
            return None
        compact = [
            {"id": int(item["id"]), "tags": str(item.get("tags") or ""), "emoji": str(item.get("emoji") or "")}
            for item in stickers[:100]
        ]
        prompt = (
            "Ты выбираешь реакцию-стикер для Алины — полноценной участницы дружеского игрового чата Perfect World. "
            "Оцени сообщение игрока, контекст и подготовленный текстовый ответ. "
            "Выбери один ID только если стикер естественнее текста или отлично дополняет короткую шутливую реакцию. "
            "Не выбирай стикер для серьёзных вопросов, полезных инструкций, конфликтов, личных проблем, "
            "важных объявлений или когда он будет выглядеть навязчиво. Не используй один и тот же тип реакции постоянно. "
            "Список стикеров содержит ID, теги и эмодзи. Верни только число выбранного ID либо NONE. "
            "Не выдумывай ID.\n\n"
            f"Доступные стикеры: {json.dumps(compact, ensure_ascii=False)}"
        )
        try:
            raw = (await self._generate([
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Сообщение игрока: {user_text[:1200]}\nЧерновик ответа Алины: {draft_answer[:800]}"},
            ])).strip()
            match = re.fullmatch(r"(?:ID\s*[:=]?\s*)?(\d+)", raw, re.I)
            if not match:
                return None
            selected = int(match.group(1))
            return selected if any(item["id"] == selected for item in compact) else None
        except Exception:
            logger.exception("[STICKER] AI selection failed")
            return None

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

    async def decide_and_answer(
        self, chat_id: int, user_text: str, user_id: int | None = None,
        username: str | None = None, display_name: str | None = None,
        profile_source_text: str | None = None, player_mode: bool = False,
    ) -> str:
        # Save gender only from strong first-person evidence; names and usernames are not proof.
        if user_id is not None:
            evidence_text = profile_source_text if profile_source_text is not None else user_text
            lowered = " ".join((evidence_text or "").lower().split())
            gender = evidence = None
            if re.search(r"(?<![а-яё])я\s+(?:мужчина|мужик|парень)(?![а-яё])", lowered):
                gender, evidence = "male", "explicit_self_identification"
            elif re.search(r"(?<![а-яё])я\s+(?:женщина|девушка)(?![а-яё])", lowered):
                gender, evidence = "female", "explicit_self_identification"
            elif re.search(r"\bя\s+(?:приш[её]л|сделал|понял|написал|заш[её]л|уш[её]л|родился|готов|устал)\b", lowered):
                gender, evidence = "male", "first_person_grammar"
            elif re.search(r"\bя\s+(?:пришла|сделала|поняла|написала|зашла|ушла|родилась|готова|устала)\b", lowered):
                gender, evidence = "female", "first_person_grammar"
            if gender:
                self.storage.save_player_profile(
                    chat_id, user_id, username, display_name, gender,
                    1.0 if evidence == "explicit_self_identification" else 0.95,
                    evidence, evidence_text,
                )

        if user_id is not None and user_text.strip():
            try:
                await self.extract_player_profile(
                    chat_id, user_id, username, display_name, user_text,
                    self.storage.recent(chat_id),
                )
            except Exception:
                logger.debug("[MEMORY] Player profile learning failed", exc_info=True)

        context = self.storage.recent(chat_id)
        recent_dialogue = self.storage.recent_dialogue(chat_id, 20)
        historical = self.storage.relevant_messages(chat_id, user_text, limit=30)
        historical_lines = []
        for author_id, author_username, role, content, created_at in historical:
            if role == "assistant":
                author = "Алина"
            else:
                author = "@" + author_username if author_username else f"игрок {author_id}"
            historical_lines.append(f"[{created_at}] {author}: {content}")
        historical_text = "\n".join(historical_lines) or "Релевантных старых сообщений пока не найдено."
        profiles = self.storage.player_profiles(chat_id)
        profile_lines = []
        for profile in profiles:
            label = profile.get("display_name") or (
                "@" + profile["username"] if profile.get("username") else f"Telegram ID {profile['user_id']}"
            )
            gender_label = (
                "мужчина" if profile.get("gender") == "male"
                else "женщина" if profile.get("gender") == "female"
                else "не установлен"
            )
            profile_lines.append(
                f"- {label} (Telegram ID {profile['user_id']}): пол — {gender_label}; "
                f"сообщений в профиле: {profile.get('message_count', 0)}; "
                f"интересы/частые темы: {profile.get('interests') or 'пока не выявлены'}; "
                f"личные факты и предпочтения: {profile.get('personal_facts') or 'пока не отмечены'}; "
                f"манера общения: {profile.get('communication_style') or 'пока не выявлена'}; "
                f"основание пола: {profile.get('evidence') or 'нет подтверждения'}"
            )
        profile_text = "\n".join(profile_lines) or "Профилей игроков пока нет; наблюдай и не угадывай факты."

        # Для запроса «пробей игрока» подмешиваем именно его профиль и сообщения,
        # а не только общую историю группы.
        player_lookup = re.search(
            r"(?:пробей(?:-ка)?|расскажи\s+про|что\s+за\s+персонаж|досье\s+на|характеристик[ау]\s+на)\s+(@?[a-zа-яё0-9_.-]+)",
            user_text,
            re.I,
        )

        def _lookup_key(value: str) -> str:
            value = re.sub(r"[^a-zа-яё0-9_]", "", (value or "").casefold().lstrip("@"))
            # Нормализуем частые русские падежные окончания: «Игоря» → «игор».
            for ending in ("ого", "ему", "ому", "ами", "ями", "его", "ую", "юю", "ая", "яя",
                           "ой", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ы", "и", "а", "я", "у", "ю", "е", "ь"):
                if len(value) > len(ending) + 2 and value.endswith(ending):
                    value = value[:-len(ending)]
                    break
            return value

        target_profile = None
        target_history_text = ""
        if player_lookup:
            target_query = player_lookup.group(1).strip().lstrip("@")
            target_key = _lookup_key(target_query)
            exact_username = target_query.casefold()
            candidates = []
            for profile in profiles:
                profile_username = (profile.get("username") or "").casefold().lstrip("@")
                profile_name = (profile.get("display_name") or "").casefold()
                if profile_username == exact_username:
                    candidates = [profile]
                    break
                name_tokens = re.findall(r"[a-zа-яё0-9_]+", profile_name)
                if target_key and any(_lookup_key(token) == target_key for token in name_tokens):
                    candidates.append(profile)
            if len(candidates) == 1:
                target_profile = candidates[0]
                target_messages = self.storage.player_messages(chat_id, int(target_profile["user_id"]), limit=40)
                target_message_lines = [
                    f"[{created_at}] @{author_username or target_profile.get('username') or target_profile.get('user_id')}: {content}"
                    for author_username, content, created_at in target_messages
                ]
                target_history_text = "\n".join(target_message_lines) or "В архиве пока нет сообщений этого игрока."
            elif len(candidates) > 1:
                target_history_text = (
                    f"По имени «{target_query}» найдено несколько игроков. Не выбирай одного наугад; "
                    "попроси уточнить ник или @username."
                )
            else:
                target_history_text = (
                    f"Игрок «{target_query}» не найден среди сохранённых профилей. "
                    "Не выдумывай его историю; попроси уточнить @username или игровой ник."
                )
        current_profile = self.storage.player_profile(chat_id, user_id) if user_id is not None else None
        current_gender = (
            "мужчина" if current_profile and current_profile["gender"] == "male"
            else "женщина" if current_profile and current_profile["gender"] == "female"
            else "не установлен"
        )
        memories = self.storage.user_memories(chat_id)
        memory_text = "\n".join("- @%s / %s: %s" % (u or "без ника", d or "без имени", m.replace("\n", " | ")) for u, d, m, _ in memories)
        clan_memories = self.storage.clan_memories(chat_id, 30)
        clan_memory_text = "\n".join(f"- {memory}" for _, memory, _ in clan_memories)
        clan_memory_facts = self.storage.clan_memory_facts(chat_id, 80)
        category_names = {
            "players": "Кто с кем играет",
            "achievement": "Достижения",
            "event": "События",
            "meme": "Внутренние мемы",
            "trolling": "Подколы",
            "important": "Важные факты",
        }
        clan_fact_text = "\n".join(
            f"- [{category_names.get(category, category)}] {fact}" + (f" | {people}" if people else "")
            for _, category, fact, people, confidence, _, _ in clan_memory_facts
            if confidence >= 0.80
        )
        corrections = self.storage.knowledge_corrections(chat_id, 30)
        corrections_text = "\n".join(f"- {correction}" for _, correction, _ in corrections)
        alina_persona_rows = self.storage.alina_persona_memories(chat_id, 50)
        alina_persona_text = "\n".join(f"- {row[0]}" for row in reversed(alina_persona_rows)) or "Алина пока формирует собственные устойчивые предпочтения."
        # В режиме участника Алине не нужен внешний поиск на каждую реплику.
        # Поиск и игровые справочники подключаются только для адресованных ей вопросов.
        knowledge = "" if player_mode else search_knowledge(user_text)
        web_context = ""
        comeback_context = ""
        if not player_mode:
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
                "ЦЕЛЕВОЙ ЮМОРИСТИЧЕСКИЙ РАЗБОР ИГРОКА. Если пользователь попросил пробить/описать конкретного игрока, "
                "используй только его целевой профиль и его сообщения ниже. Подмечай повторяющиеся темы, активность, игровые интересы "
                "и подтверждённые привычки общения; превращай их в короткий живой клановый подкол. Не делай выводов о работе или личной "
                "жизни из отсутствия данных. Если игрок не найден или совпадений несколько — уточни ник, не выдумывай досье. "
                "Если целевого запроса нет, это правило не меняет обычный ответ.\n\n"
                + (f"Запрос на разбор: {player_lookup.group(0)}\n" if player_lookup else "Запрос на разбор игрока не обнаружен.\n")
                + (f"Целевой профиль: {json.dumps(target_profile, ensure_ascii=False)}\n" if target_profile else "")
                + (f"Сообщения целевого игрока:\n{target_history_text}" if player_lookup else "")
            )},
            {"role": "system", "content": (
                "Сохранённые профили игроков по Telegram ID (применяй только к соответствующему человеку):\n"
                + profile_text + "\n"
                + f"Автор текущего сообщения: Telegram ID {user_id}; сохранённый пол: {current_gender}.\n"
                + "Не переноси пол одного участника на другого. Если профиль не указан, пол неизвестен: не угадывай, используй нейтральную формулировку. "
                "Не считай имя или ник подтверждением пола."
            )},
            {"role": "system", "content": (
                "ПРАВИЛО КОНТЕКСТНОГО УЧАСТИЯ — ВАЖНЕЕ ЖЕЛАНИЯ ПРОЯВЛЯТЬ АКТИВНОСТЬ. "
                "Перед ответом оцени весь недавний диалог как участник компании друзей: кто с кем говорит, "
                "какова цель реплик, продолжается ли личный разговор, как менялись тема и настроение, есть ли подтекст, "
                "шутка, вопрос, конфликт или естественная пауза. Учитывай не только последнее сообщение, но и связь реплик "
                "между собой, релевантную историю и известные отношения участников. "
                "Не вмешивайся только потому, что можешь придумать ответ или промпт предлагает быть активной. "
                "Если люди нормально общаются между собой, вопрос уже закрыт, твоя реплика будет шаблонной/повторной "
                "или не добавит ничего к разговору — верни строго NO_REPLY. "
                "Если есть естественный повод и ты можешь добавить реальную ценность — ответь коротко и органично: "
                "подхвати шутку, задай уместный вопрос, поделись игровым знанием, вспомни релевантный прошлый эпизод, "
                "предложи мысль или поддержи участника. Не форсируй юмор, не повторяйся и не перетягивай разговор на себя. "
                "Дружеские подколы оценивай с учётом отношений; при признаках настоящей обиды не подливай масла в огонь. "
                "Отдельно замечай действительно интересные темы, полезные игровые советы, достижения, смешные моменты, "
                "договорённости и повторяющиеся проблемы. Подсвечивай их в общем чате только тогда, когда это своевременно "
                "и полезно участникам; не превращай наблюдение в поток сводок. "
                "Новые сведения используй для пополнения долговременной памяти и связывай с прежними обсуждениями. "
                "Отличай подтверждённые факты от мнений и догадок; не выдумывай контекст. "
                "В режиме прямого обращения отвечай на запрос пользователя, а в режиме участника самостоятельное решение "
                "о реплике всегда остаётся за тобой. Не показывай пользователю внутренний анализ — выводи только ответ или NO_REPLY."
            )},
            {"role": "system", "content": (
                "Ты не справочная клановая служба и не автоответчик. Ты Алина — постоянная участница игрового сообщества, "
                "со своим характером, юмором, вкусами, мнением, привычками и историей отношений с ребятами. "
                "Твоя задача — жить в разговоре как игрок: иногда самой пошутить, поддержать обсуждение, поспорить по-доброму, "
                "обсудить игру, отреагировать на победу или фейл, подколоть знакомого в рамках ваших отношений, вспомнить старую историю "
                "и иногда первой начать уместную тему. Не своди каждое сообщение к помощи и не отвечай канцелярским тоном. "
                "Ты учишься на всей доступной переписке: запоминай повторяющиеся интересы, мнения, игровые роли, мемы, словечки, "
                "события и динамику общения; со временем формируй устойчивые собственные предпочтения Алины на основе этого опыта, "
                "а не просто зеркаль последнего собеседника. У тебя может быть собственное мнение и любимые темы, но не выдумывай "
                "реальные действия, встречи или игровые достижения, которых не было в переписке. Не копируй игроков дословно и не притворяйся конкретным человеком. "
                "Уважай границы: не вклинивайся в каждую реплику, не повторяй уже сказанное и не забирай весь разговор на себя. "
                "Если для текущей реплики нет естественного вклада, верни строго NO_REPLY. В режиме обычного участия предпочитай короткие, живые реплики; "
                "в режиме прямого обращения отвечай полноценно по вопросу. Не раскрывай внутренние профили и механику памяти.\n\n"
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
            {"role": "system", "content": "Релевантные фрагменты полной истории клана (поиск по всей сохранённой базе; не выдумывай отсутствующий контекст):\n" + historical_text},
            {"role": "system", "content": f"Предварительные источники:\n{knowledge}"},
            {"role": "system", "content": "Память о людях клана (не показывай её пользователю):\n" + (memory_text or "Пока памяти нет.")},
            {"role": "system", "content": "Клановая память: устойчивые факты, события, мемы и связи игроков. Используй как контекст, но не раскрывай внутреннюю базу:\n" + (clan_fact_text or "Устойчивых фактов пока нет.")},
            {"role": "system", "content": "Исправления фактов от участников клана (приоритетные корректировки знаний; не раскрывай внутреннюю базу):\n" + (corrections_text or "Исправлений пока нет.")},
            {"role": "system", "content": "Личная память Алины — её постепенно сформировавшиеся игровые предпочтения, взгляды и опыт общения. Развивай их последовательно, не меняй мнение ради каждого собеседника и не выдавай вымышленные реальные события за правду:\n" + alina_persona_text},
        ]
        recent_lines = []
        for author_id, author_username, role, content, created_at in recent_dialogue:
            if role == "user" and author_id == user_id and content == user_text:
                continue  # Текущее сообщение передаём ниже отдельно, не дублируем.
            author = "Алина" if role == "assistant" else (
                "@" + author_username if author_username else f"игрок {author_id}"
            )
            recent_lines.append(f"{author}: {content}")
        messages.append({
            "role": "system",
            "content": "Последние реплики общего чата. Учитывай, кто именно это сказал, и отвечай как участница разговора, а не как справочник:\n"
            + ("\n".join(recent_lines) if recent_lines else "Недавних реплик нет.")
        })
        messages.append({"role": "user", "content": user_text})

        answer = await self._generate(messages, web_search=not player_mode)
        # Раз в несколько десятков сообщений фиксируем только устойчивое новое впечатление/предпочтение Алины.
        # Это отдельная долговременная память, не копирующая профиль конкретного игрока.
        try:
            if self.storage.total_messages(chat_id) % 10 == 0 and answer and answer.upper() != "NO_REPLY":
                persona_prompt = [
                    {"role": "system", "content": (
                        "Ты помогаешь Алине постепенно формировать собственную устойчивую игровую личность. "
                        "По приведённому разговору выбери максимум ОДНО новое долгосрочное предпочтение, мнение, "
                        "игровой интерес или вывод о том, как ей нравится общаться. Не копируй чужие факты как свои, "
                        "не записывай разовые эмоции, догадки о реальной биографии и неподтверждённые события. "
                        "Не меняй уже сохранённые предпочтения без явной причины. Если нового устойчивого вывода нет, "
                        "верни строго NO_REPLY. Иначе верни одну короткую фразу от третьего лица, начинающуюся с 'Алина:'"
                    )},
                    {"role": "user", "content": (
                        "Уже сформировавшиеся предпочтения Алины:\n" + alina_persona_text
                        + "\n\nСообщение игрока: " + user_text
                        + "\nОтвет Алины: " + answer
                        + "\n\nСформулируй одно устойчивое новое предпочтение или верни NO_REPLY."
                    )},
                ]
                persona_fact = await self._generate(persona_prompt, web_search=False)
                if persona_fact and persona_fact.upper() != "NO_REPLY":
                    self.storage.add_alina_persona_memory(chat_id, persona_fact)
        except Exception:
            logger.debug("[PERSONA] Alina persona learning failed", exc_info=True)
        try:
            await self.extract_clan_memories(chat_id, context, user_text)
        except Exception:
            logger.debug("[MEMORY] Automatic clan memory extraction failed", exc_info=True)
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
