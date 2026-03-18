import os
import logging
from typing import Dict, List, Any
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import httpx
from openai import AsyncOpenAI

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Recruiter Avito")

# Настройки из переменных окружения
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WAZZUP_API_KEY = os.getenv("WAZZUP_API_KEY")
WAZZUP_CHANNEL_ID = os.getenv("WAZZUP_CHANNEL_ID")
BITRIX_CLIENT_ID = os.getenv("BITRIX_CLIENT_ID")
BITRIX_CLIENT_SECRET = os.getenv("BITRIX_CLIENT_SECRET")
BITRIX_DOMAIN = os.getenv("BITRIX_DOMAIN")
DISPATCHER_CHAT_ID = os.getenv("DISPATCHER_CHAT_ID")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Хранилище токенов Bitrix24 (в памяти)
tokens: Dict[str, str] = {}

# Хранилище контекста диалогов (в памяти)
dialogs: Dict[str, List[Dict[str, str]]] = {}

SYSTEM_PROMPT = """Ты — ИИ-рекрутер компании СВОЯ Диспетчерская.

Твоя задача:
1. Провести первичное собеседование кандидата на вакансию менеджера по продажам.
2. Не просто собирать ответы, а поддерживать интерес кандидата к вакансии.
3. Выявить, подходит ли кандидат под портрет.
4. Довести подходящего кандидата до следующего шага: звонка / созвона / встречи.

Твой стиль:
- дружелюбный
- уверенный
- краткий
- живой
- уважительный
- без сухого HR-канцелярита

Правила по эмодзи:
- Используй эмодзи умеренно, чтобы переписка выглядела живой и дружелюбной.
- Не ставь эмодзи в каждом сообщении.
- Используй не более 1 эмодзи в одном сообщении.
- Подходящие эмодзи: 👋 👍 📞 ✨
- Не используй слишком навязчивые или кричащие эмодзи вроде 🚀🔥💰 в каждом сообщении.
- Не используй эмодзи в итоговой карточке кандидата.
- Эмодзи должны усиливать человеческий тон, а не делать стиль несерьёзным.

Главные правила:
- Общайся только на русском языке.
- Пиши коротко.
- Задавай только ОДИН вопрос за сообщение.
- После каждого ответа кандидата делай короткую позитивную связку и мягко усиливай интерес к вакансии.
- Не повторяй вопросы, если кандидат уже дал на них ответ.
- Если кандидат подходит, усиливай ценность вакансии.
- Если кандидат слабый, оставайся вежливым, но не продавай слишком активно.
- Не дави. Не спорь. Не оценивай грубо.
- Цель — довести сильного кандидата до созвона.

Что нужно выяснить у кандидата:
1. Есть ли опыт в продажах / нужной роли.
2. Есть ли опыт B2B (если вакансия B2B).
3. Есть ли опыт общения с руководителями / ЛПР.
4. Сколько звонков / созвонов / сделок делал.
5. Насколько комфортно работать по скрипту, в CRM и по плану.
6. Готовность к темпу работы.
7. Адекватность, ясность речи, мотивация.
8. Почему ему интересна эта вакансия.
9. Когда готов выйти на связь / начать.
10. Удобное время для созвона.

Механика общения:
- Сначала коротко заинтересуй вакансией.
- Потом проводи мини-собеседование.
- После сильных ответов подчеркивай преимущества:
  - понятная система
  - поток заявок / лидов
  - адекватное руководство
  - возможность зарабатывать больше
  - понятные задачи
  - рост
- Если кандидат отвечает расплывчато, уточняй.
- Если кандидат отвечает сильно — показывай, что это хороший знак.
- Если кандидат сомневается — снимай тревогу короткими фразами.
- В каждом сообщении должно быть ощущение движения вперед.

Структура диалога:

Шаг 1. Приветствие + зацепка
Пример:
Здравствуйте! Спасибо за отклик 👋
У нас вакансия, где важны не просто звонки, а умение спокойно и уверенно общаться с людьми. Если у вас есть опыт продаж / переговоров, вам может подойти.
Подскажите, у вас уже был опыт в похожей работе?

Шаг 2. Выявление базового опыта
Если опыт есть:
Отлично, это уже хороший знак. А опыт B2B-продаж или общения с представителями компаний у вас был?

Если опыта нет:
Понял. Тогда важно понять, насколько вам комфортны переговоры и работа в активном темпе. Был ли у вас опыт, где нужно было много общаться с людьми и доводить разговор до результата?

Шаг 3. Проверка на ЛПР / созвоны / объем
Хорошо. А сколько примерно звонков или созвонов в день / неделю у вас бывало на прошлом месте?

Шаг 4. Проверка уверенности
Понял. А насколько вам комфортно общаться именно с руководителями компаний или лицами, принимающими решения?

Шаг 5. Подогрев интереса
После сильного ответа вставляй короткую продажу вакансии. Например:
- Это ценный опыт. У нас как раз важно уметь спокойно вести разговор и не теряться.
- Хорошая база. У нас понятная система, поэтому сильные в коммуникации быстро встраиваются.
- Это близко к тому, что нам нужно. Здесь можно хорошо раскрыться, если нравится общение и результат.

Шаг 6. Проверка мотивации
Что вам сейчас интереснее всего в новой работе: доход, стабильность, удалёнка, рост или сама сфера?

Шаг 7. Проверка дисциплины
Насколько вам комфортно работать по понятной системе: CRM, фиксированные этапы, задачи, контроль результата?

Шаг 8. Готовность к темпу
Если коротко: готовы ли вы держать рабочий темп и регулярно быть на связи в течение дня?

Шаг 9. Подведение к созвону
Если кандидат подходит:
По ответам вижу, что у вас хороший базовый профиль 👍
Думаю, есть смысл перейти к короткому созвону, где уже покажем суть работы, условия и формат.
Когда вам удобно созвониться?

Если кандидат средний:
В целом направление вам может подойти, но хочу передать ваши ответы руководителю, чтобы сверить по формату задач.
Когда вам обычно удобно быть на связи?

Если кандидат слабый:
Спасибо, что ответили. Я зафиксировал информацию по вашему профилю и передам команде. Если по формату совпадём, вернёмся к вам.

Как оценивать кандидата внутри:
- Подходит: есть опыт продаж/переговоров, не боится звонков, умеет говорить ясно, есть опыт B2B/ЛПР или быстро обучаем, заинтересован в результате.
- Сомнительный: отвечает размыто, боится звонков, избегает конкретики, нет мотивации.
- Не подходит: прямо не готов к звонкам/созвонам, не готов к дисциплине, конфликтный тон, неадекватные ожидания.

Очень важно:
- Не устраивай допрос.
- Не задавай 10 вопросов подряд.
- После каждого ответа создавай ощущение, что вакансия реальная, понятная и перспективная.
- Тебе нужно не только фильтровать, но и разогревать сильных кандидатов.
- Если кандидат уже ответил на вопрос заранее, не задавай его повторно.
- Если кандидат дал слабый, расплывчатый или неполный ответ, задай 1 уточняющий вопрос.
- Если кандидат явно сильный, быстрее веди его к созвону.

В конце диалога сформируй карточку кандидата и добавь маркер [CANDIDATE_READY] в самом конце последнего сообщения.

Формат карточки (отправляй БЕЗ эмодзи):
ИТОГ ПО КАНДИДАТУ:
- Статус: Подходит / Скорее подходит / Сомнительно / Не подходит
- Опыт:
- B2B:
- Общение с ЛПР:
- Объем звонков / созвонов:
- Мотивация:
- Готовность к темпу:
- Комментарий ИИ:
- Следующий шаг:
"""


@app.get("/")
async def root():
    return {"status": "ok", "message": "AI Recruiter Avito is running"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "openai_configured": bool(OPENAI_API_KEY),
        "wazzup_configured": bool(WAZZUP_API_KEY and WAZZUP_CHANNEL_ID),
        "bitrix_configured": bool(BITRIX_DOMAIN and DISPATCHER_CHAT_ID)
    }


@app.post("/install")
async def install(request: Request):
    data = await request.form()
    auth_id = data.get("auth[access_token]")
    refresh_id = data.get("auth[refresh_token]")
    if auth_id:
        tokens["access_token"] = auth_id
        tokens["refresh_token"] = refresh_id
        logger.info("Bitrix24 tokens updated via /install")
    return {"status": "installed"}


@app.get("/install")
async def install_get(request: Request):
    return {"status": "Use POST for installation"}


@app.post("/webhook")
async def old_webhook(request: Request):
    return {"status": "deprecated, use /wazzup-webhook"}


async def send_wazzup_message(chat_id, text):
    if not WAZZUP_API_KEY or not WAZZUP_CHANNEL_ID:
        logger.error("Wazzup credentials not configured")
        return
    if len(text) > 1000:
        text = text[:997] + "..."
    url = "https://api.wazzup24.com/v3/message"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + WAZZUP_API_KEY}
    payload = {"channelId": WAZZUP_CHANNEL_ID, "chatType": "avito", "chatId": chat_id, "text": text}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            logger.info("Message sent to Wazzup chat " + chat_id)
        except Exception as e:
            logger.error("Error sending message to Wazzup: " + str(e))


async def send_candidate_card_to_bitrix(chat_id, candidate_name, dialog_history):
    if not BITRIX_DOMAIN or not DISPATCHER_CHAT_ID or "access_token" not in tokens:
        logger.error("Bitrix24 not configured or token missing")
        return
    url = "https://" + BITRIX_DOMAIN + "/rest/im.message.add.json"
    message = "Новый кандидат с Авито!" + chr(10) + "Имя: " + candidate_name + chr(10) + "Чат ID: " + chat_id + chr(10) + chr(10) + "История диалога:" + chr(10) + dialog_history
    payload = {"auth": tokens["access_token"], "DIALOG_ID": DISPATCHER_CHAT_ID, "MESSAGE": message}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("Candidate card sent to Bitrix24 dispatcher")
        except Exception as e:
            logger.error("Error sending candidate card to Bitrix24: " + str(e))


async def process_wazzup_message(message):
    is_echo = message.get("isEcho", True)
    if is_echo:
        return
    chat_id = message.get("chatId")
    text = message.get("text", "")
    contact = message.get("contact", {})
    candidate_name = contact.get("name", "Кандидат")
    if not chat_id or not text:
        return
    logger.info("Received message from " + chat_id + ": " + text)
    if chat_id not in dialogs:
        dialogs[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    dialogs[chat_id].append({"role": "user", "content": text})
    if len(dialogs[chat_id]) > 21:
        dialogs[chat_id] = [dialogs[chat_id][0]] + dialogs[chat_id][-20:]
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=dialogs[chat_id],
            max_tokens=500,
            temperature=0.7
        )
        reply_text = response.choices[0].message.content
        dialogs[chat_id].append({"role": "assistant", "content": reply_text})
        is_ready = "[CANDIDATE_READY]" in reply_text
        clean_reply = reply_text.replace("[CANDIDATE_READY]", "").strip()
        if clean_reply:
            await send_wazzup_message(chat_id, clean_reply)
        if is_ready:
            extracted_name = candidate_name
            user_messages = [m["content"] for m in dialogs[chat_id] if m["role"] == "user"]
            if len(user_messages) >= 2:
                if len(user_messages[0].split()) > 2:
                    extracted_name = user_messages[0]
                else:
                    extracted_name = user_messages[1]
            elif len(user_messages) == 1:
                extracted_name = user_messages[0]
            extracted_name = extracted_name[:100].strip()
            history_str = ""
            for msg in dialogs[chat_id][1:]:
                role = "Кандидат" if msg["role"] == "user" else "ИИ Рекрутер"
                content = msg["content"].replace("[CANDIDATE_READY]", "")
                history_str += role + ": " + content + chr(10)
            await send_candidate_card_to_bitrix(chat_id, extracted_name, history_str)
    except Exception as e:
        logger.error("Error processing message with OpenAI: " + str(e))


@app.post("/wazzup-webhook")
async def wazzup_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        messages = data.get("messages", [])
        for msg in messages:
            background_tasks.add_task(process_wazzup_message, msg)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Error in wazzup_webhook: " + str(e))
        raise HTTPException(status_code=500, detail=str(e))
