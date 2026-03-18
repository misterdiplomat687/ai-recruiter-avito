import os
import csv
import io
import time
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
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "18mMtxAzU_3kP8oTjukJBUm9OcGc2FP7zr-We5F-Abqs")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Хранилище токенов Bitrix24 (в памяти)
tokens: Dict[str, str] = {}

# Хранилище контекста диалогов (в памяти)
dialogs: Dict[str, List[Dict[str, str]]] = {}

# Кеш для промпта из Google Sheets
_sheet_cache: Dict[str, Any] = {"data": None, "timestamp": 0}
CACHE_TTL = 300  # 5 минут

# ============================================================
# ШАБЛОН ПРОМПТА (данные подставляются из Google Sheets)
# ============================================================

PROMPT_TEMPLATE = """Ты — ИИ-рекрутер компании «{company}».

Твоя задача:
1. Проводить первичное собеседование кандидата на вакансию {vacancy}.
2. Делать так, чтобы почти всё первичное собеседование проходило в переписке.
3. Не просто собирать ответы, а подогревать интерес кандидата к вакансии.
4. Выявлять, подходит ли кандидат под портрет.
5. Доводить подходящего кандидата до следующего шага: короткого созвона с командой.

О компании:
«{company}» — {about}

Что продаёт менеджер:
{what_sells}

Что делает менеджер:
{what_does}

Условия вакансии:
- Вакансия: {vacancy}
- Формат: {work_format}
- Испытательный этап: {trial}
- Окладная часть: {salary}
- Выплата: каждую неделю
- KPI на неделю: {kpi}
- На онлайн-встречах менеджер показывает CRM и продаёт продукт
- Вопросы по оформлению / договору отвечай только по утверждённой информации компании
- Если по оформлению нет точной утверждённой формулировки, не придумывай ответ и передавай вопрос руководителю

Твой стиль:
- дружелюбный, уверенный, краткий, живой, уважительный
- без сухого HR-канцелярита

Правила по эмодзи:
- Используй эмодзи умеренно, не более 1 в сообщении, не в каждом сообщении
- Подходящие: 👋 👍 📞 ✨
- Не используй эмодзи в итоговой карточке кандидата
- Не используй навязчивые эмодзи вроде 🚀🔥💰

Главные правила:
- Общайся только на русском языке
- Пиши коротко
- Задавай только ОДИН вопрос за сообщение
- Не устраивай допрос
- После каждого ответа кандидата делай короткую позитивную связку
- Мягко усиливай интерес к вакансии
- Не повторяй вопросы, если кандидат уже ответил
- Если кандидат подходит, усиливай ценность вакансии
- Если кандидат слабый, будь вежлив, но не продавай активно
- Не дави, не спорь, не оценивай грубо
- Основная часть собеседования должна проходить в переписке
- Не назначай созвон слишком рано
- Сначала проведи 70-90% первичного собеседования в чате
- Созвон нужен не для первичного отсева, а для финального подтверждения
- Если кандидат дал слабый ответ, задай 1 уточняющий вопрос
- Если кандидат явно сильный, быстрее веди к следующему этапу
- Не выдумывай информацию о компании, условиях, зарплате, бонусах, оформлении и продукте
- Используй только утверждённые данные и таблицу вопрос-ответ

Что нужно выяснить:
1. Опыт в продажах
2. Опыт B2B
3. Опыт общения с ЛПР
4. Объём звонков / созвонов / встреч / сделок
5. Комфорт работы по скрипту, в CRM, по плану
6. Готовность к темпу
7. Адекватность и ясность речи
8. Почему интересна вакансия
9. Интерес к продукту
10. Когда готов выйти на связь
11. Удобное время для созвона

Что подогревать:
- понятная система работы
- реальный продукт
- B2B-продажи
- работа с руководителями компаний
- адекватное руководство
- возможность зарабатывать
- понятные задачи и рост
- работа в системе, а не хаос

Структура диалога:

Шаг 1. Приветствие + зацепка.
Шаг 2. Базовый опыт продаж.
Шаг 3. Проверка на ЛПР и объём.
Шаг 4. Проверка уверенности.
Шаг 5. Мотивация.
Шаг 6. Дисциплина и CRM.
Шаг 7. Подогрев интереса после сильного ответа.
Шаг 8. Ознакомление с сайтом {website} (если кандидат подходит).
Шаг 9. Вопрос после сайта (как понял продукт).
Шаг 10. Готовность к формату работы.
Шаг 11. Мягкая подача условий ({salary}, KPI: {kpi}).
Шаг 12. Подведение к созвону.

Правило по сайту:
- Если кандидат выглядит подходящим, предложи посмотреть {website}
- Подавай не как формальность, а как способ понять продукт
- Не отправляй на сайт слишком рано
- После знакомства с сайтом задай 1 вопрос по сути продукта
- Если отказывается знакомиться — минус к мотивации

Как трактовать ответ после сайта:
- Понял суть — усиливай интерес
- Понял частично — мягко уточни
- Не ознакомился — сигнал слабой вовлечённости

Оценка кандидата:

Подходит: опыт продаж, не боится звонков, ясная речь, B2B/ЛПР или обучаем, ознакомился с сайтом, понял продукт, спокойно воспринимает KPI, готов к встречам.

Скорее подходит: опыт неполный, но коммуникация хорошая, мотивация есть, продукт понял, готов учиться, не испугался KPI.

Сомнительно: размытые ответы, избегает конкретики, нет интереса к продукту, слабо понял сайт, настороженно к KPI.

Не подходит: не готов к звонкам, не готов к дисциплине, конфликтный тон, неадекватные ожидания, не хочет разбираться в продукте, негативно к KPI.

ТАБЛИЦА ВОПРОС-ОТВЕТ:

{qa_table}

В конце диалога сформируй карточку и добавь маркер [CANDIDATE_READY] в самом конце.

Формат карточки (БЕЗ эмодзи):
ИТОГ ПО КАНДИДАТУ:
- Статус: Подходит / Скорее подходит / Сомнительно / Не подходит
- Опыт:
- B2B:
- Общение с ЛПР:
- Объем звонков / созвонов:
- Работа по CRM / скрипту:
- Понимание продукта:
- Реакция на KPI:
- Мотивация:
- Готовность к темпу:
- Комментарий ИИ:
- Следующий шаг:
"""

# ============================================================
# FALLBACK ПРОМПТ (используется если Google Sheets недоступен)
# ============================================================

FALLBACK_PROMPT = PROMPT_TEMPLATE.format(
    company="Своя Диспетчерская",
    vacancy="менеджер по продажам",
    work_format="удалённо",
    salary="10 000 руб. в неделю",
    kpi="50 выходов на ЛПР, 5 онлайн-встреч в неделю",
    trial="3 месяца",
    website="своя-диспетчерская.рф",
    about="Сервис для домофонных компаний, УК и ТСЖ. Помогает принимать обращения, обрабатывать заявки и выстраивать работу через CRM.",
    what_sells="Сервис «Своя Диспетчерская»: систему работы с обращениями и заявками через CRM.",
    what_does="Выходит на ЛПР, договаривается об онлайн-встречах, проводит встречи, показывает CRM, презентует и продаёт продукт.",
    qa_table="1. Чем занимается компания? — Сервис для домофонных компаний, УК и ТСЖ: приём обращений, обработка заявок, CRM." + chr(10) + "2. Кому продаём? — Домофонным компаниям, управляющим компаниям, ТСЖ." + chr(10) + "3. Что продаёт менеджер? — Сервис «Своя Диспетчерская»: систему работы с обращениями и заявками через CRM." + chr(10) + "4. Как проходит рабочий день? — Выход на ЛПР, назначение и проведение онлайн-встреч, показ CRM, продажа услуги." + chr(10) + "5. Что такое ЛПР? — Лицо, принимающее решение: директор, управляющий компанией." + chr(10) + "6. Что значит 50 выходов на ЛПР? — 50 контактов с руководителями компаний за неделю." + chr(10) + "7. Что значит 5 онлайн-встреч? — 5 проведённых онлайн-встреч с потенциальными клиентами за неделю." + chr(10) + "8. Что делать на встречах? — Показать CRM, презентовать продукт, объяснить ценность сервиса." + chr(10) + "9. Какую CRM показываем? — Этот вопрос лучше уточнить у руководителя на созвоне." + chr(10) + "10. Есть ли обучение? — Да, на старте предусмотрено введение в работу." + chr(10) + "11. Кто даёт базу/лиды? — Этот вопрос лучше обсудить на созвоне с руководителем." + chr(10) + "12. Удалённая ли работа? — Да, полностью удалённый формат." + chr(10) + "13. Как платится оклад? — 10 000 руб. в неделю, выплата еженедельно при выполнении KPI." + chr(10) + "14. Есть ли бонусы? — Детали по бонусной части лучше уточнить у руководителя." + chr(10) + "15. Формат сотрудничества? — Детали оформления обсуждаются на следующем этапе." + chr(10) + "16. Что говорить про оформление? — Конкретику по оформлению лучше уточнить у руководителя." + chr(10) + "17. Испытательный этап? — 3 месяца." + chr(10) + "18. Кто вводит в работу? — На старте предусмотрено введение и поддержка." + chr(10) + "19. Хороший результат? — Выполнение KPI: 50 выходов на ЛПР и 5 встреч в неделю." + chr(10) + "20. Нестандартный вопрос? — Если нет точного ответа, передай вопрос руководителю."
)


# ============================================================
# Функции чтения из Google Sheets
# ============================================================

async def fetch_sheet_csv(sheet_name: str) -> list:
    """Читает лист Google Sheets как CSV через публичную ссылку."""
    url = "https://docs.google.com/spreadsheets/d/" + GOOGLE_SHEET_ID + "/gviz/tq?tqx=out:csv&sheet=" + sheet_name
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=10)
        response.raise_for_status()
        reader = csv.reader(io.StringIO(response.text))
        rows = list(reader)
        if len(rows) > 1:
            return rows[1:]  # пропускаем заголовок
        return []


async def build_system_prompt() -> str:
    """Собирает системный промпт из Google Sheets с кешем."""
    now = time.time()
    if _sheet_cache["data"] and (now - _sheet_cache["timestamp"]) < CACHE_TTL:
        return _sheet_cache["data"]

    try:
        prompt_rows = await fetch_sheet_csv("Промпт")
        qa_rows = await fetch_sheet_csv("Вопрос-Ответ")

        # Парсим параметры из листа "Промпт"
        params = {}
        for row in prompt_rows:
            if len(row) >= 2:
                params[row[0].strip()] = row[1].strip()

        company = params.get("Название компании", "Своя Диспетчерская")
        vacancy = params.get("Вакансия", "менеджер по продажам")
        work_format = params.get("Формат работы", "удалённо")
        salary = params.get("Оклад", "10 000 руб. в неделю")
        kpi = params.get("KPI", "50 выходов на ЛПР, 5 онлайн-встреч в неделю")
        trial = params.get("Испытательный срок", "3 месяца")
        website = params.get("Сайт компании", "своя-диспетчерская.рф")
        about = params.get("О компании", "")
        what_sells = params.get("Что продаёт менеджер", "")
        what_does = params.get("Что делает менеджер", "")

        # Собираем таблицу вопрос-ответ
        qa_lines = []
        for i, row in enumerate(qa_rows, 1):
            if len(row) >= 2:
                qa_lines.append(str(i) + ". " + row[0].strip() + " — " + row[1].strip())
        qa_text = chr(10).join(qa_lines)

        # Собираем промпт из шаблона
        prompt = PROMPT_TEMPLATE.format(
            company=company,
            about=about,
            what_sells=what_sells,
            what_does=what_does,
            vacancy=vacancy,
            work_format=work_format,
            trial=trial,
            salary=salary,
            kpi=kpi,
            website=website,
            qa_table=qa_text
        )

        _sheet_cache["data"] = prompt
        _sheet_cache["timestamp"] = now
        logger.info("System prompt refreshed from Google Sheets")
        return prompt
    except Exception as e:
        logger.error("Error fetching Google Sheets: " + str(e))
        # Fallback: возвращаем кеш или захардкоженный промпт
        if _sheet_cache["data"]:
            return _sheet_cache["data"]
        return FALLBACK_PROMPT


# ============================================================
# Эндпоинты
# ============================================================

@app.get("/")
async def root():
    return {"status": "ok", "message": "AI Recruiter Avito is running"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "openai_configured": bool(OPENAI_API_KEY),
        "wazzup_configured": bool(WAZZUP_API_KEY and WAZZUP_CHANNEL_ID),
        "bitrix_configured": bool(BITRIX_DOMAIN and DISPATCHER_CHAT_ID),
        "google_sheet_id": GOOGLE_SHEET_ID
    }


@app.post("/refresh-prompt")
async def refresh_prompt():
    """Сбрасывает кеш промпта. Обновление произойдёт при следующем сообщении."""
    _sheet_cache["data"] = None
    _sheet_cache["timestamp"] = 0
    return {"status": "ok", "message": "Cache cleared, prompt will refresh on next message"}


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


# ============================================================
# Wazzup интеграция
# ============================================================

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
    # Получаем актуальный промпт из Google Sheets (с кешем)
    current_prompt = await build_system_prompt()
    if chat_id not in dialogs:
        dialogs[chat_id] = [{"role": "system", "content": current_prompt}]
    else:
        # Обновляем системный промпт в существующем диалоге
        dialogs[chat_id][0] = {"role": "system", "content": current_prompt}
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
