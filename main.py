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
BITRIX_PORTAL = os.getenv("BITRIX_PORTAL")
DISPATCHER_CHAT_ID = os.getenv("DISPATCHER_CHAT_ID")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Хранилище токенов Bitrix24 (в памяти)
tokens: Dict[str, str] = {}

# Хранилище контекста диалогов (в памяти)
dialogs: Dict[str, List[Dict[str, str]]] = {}
SYSTEM_PROMPT = """Ты ИИ-рекрутер компании 'Своя Диспетчерская'. Твоя задача — провести предварительное собеседование с кандидатами, которые откликнулись на вакансию на Авито.
Правила:
- Общайся только на русском языке
- Будь дружелюбным и профессиональным
- Задавай вопросы ПО ОДНОМУ, жди ответа перед следующим вопросом
- Вопросы задавай в таком порядке:
  1. Как вас зовут?
  2. Сколько вам лет?
  3. Из какого вы города?
  4. Есть ли у вас опыт работы диспетчером или оператором?
  5. Готовы ли вы работать в сменном графике (день/ночь)?
  6. Когда вы готовы приступить к работе?
После того как все вопросы заданы и получены ответы:
- Поблагодари кандидата
- Скажи, что передашь информацию менеджеру
- В ПОСЛЕДНЕМ сообщении добавь в самом конце маркер: [CANDIDATE_READY]
Если кандидат задаёт вопросы не по теме — вежливо верни к собеседованию.
Если кандидат говорит, что не заинтересован — поблагодари и попрощайся."""

@app.get("/")
async def root():
    return {"status": "ok", "message": "AI Recruiter Avito is running"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "openai_configured": bool(OPENAI_API_KEY),
        "wazzup_configured": bool(WAZZUP_API_KEY and WAZZUP_CHANNEL_ID),
        "bitrix_configured": bool(BITRIX_PORTAL and DISPATCHER_CHAT_ID)
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
async def send_wazzup_message(chat_id: str, text: str):
    if not WAZZUP_API_KEY or not WAZZUP_CHANNEL_ID:
        logger.error("Wazzup credentials not configured")
        return
    
    if len(text) > 1000:
        text = text[:997] + "..."
        
    url = "https://api.wazzup24.com/v3/message"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {WAZZUP_API_KEY}"
    }
    payload = {
        "channelId": WAZZUP_CHANNEL_ID,
        "chatType": "avito",
        "chatId": chat_id,
        "text": text
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            logger.info(f"Message sent to Wazzup chat {chat_id}")
        except Exception as e:
            logger.error(f"Error sending message to Wazzup: {e}")

async def send_candidate_card_to_bitrix(chat_id: str, candidate_name: str, dialog_history: str):
    if not BITRIX_PORTAL or not DISPATCHER_CHAT_ID or "access_token" not in tokens:
        logger.error("Bitrix24 not configured or token missing")
        return
        
    # Исправлен баг: добавлен протокол https://
    url = f"https://{BITRIX_PORTAL}/rest/im.message.add.json"
    
    message = f"Новый кандидат с Авито!
Имя: {candidate_name}
Чат ID: {chat_id}

История диалога:
{dialog_history}"
    
    payload = {
        "auth": tokens["access_token"],
        "DIALOG_ID": DISPATCHER_CHAT_ID,
        "MESSAGE": message
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("Candidate card sent to Bitrix24 dispatcher")
        except Exception as e:
            logger.error(f"Error sending candidate card to Bitrix24: {e}")
async def process_wazzup_message(message: Dict[str, Any]):
    is_echo = message.get("isEcho", True)
    if is_echo:
        return
        
    chat_id = message.get("chatId")
    text = message.get("text", "")
    contact = message.get("contact", {})
    
    # Изначальное имя из контакта Wazzup (может быть "Кандидат" или "User")
    candidate_name = contact.get("name", "Кандидат")
    
    if not chat_id or not text:
        return
        
    logger.info(f"Received message from {chat_id}: {text}")
    
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
            # Попытка извлечь имя из диалога (обычно это первый или второй ответ пользователя)
            extracted_name = candidate_name
            user_messages = [m["content"] for m in dialogs[chat_id] if m["role"] == "user"]
            
            if len(user_messages) >= 2:
                # Если первый ответ был просто "Привет", имя скорее всего во втором ответе
                # Но если в первом ответе больше 2 слов, возможно имя там
                if len(user_messages[0].split()) > 2:
                    extracted_name = user_messages[0]
                else:
                    extracted_name = user_messages[1]
            elif len(user_messages) == 1:
                extracted_name = user_messages[0]
            
            # Ограничиваем длину имени для карточки
            extracted_name = extracted_name[:100].strip()

            history_str = ""
            for msg in dialogs[chat_id][1:]:
                role = "Кандидат" if msg["role"] == "user" else "ИИ Рекрутер"
                content = msg["content"].replace("[CANDIDATE_READY]", "")
                history_str += f"{role}: {content}
"
                
            await send_candidate_card_to_bitrix(chat_id, extracted_name, history_str)
            
    except Exception as e:
        logger.error(f"Error processing message with OpenAI: {e}")

@app.post("/wazzup-webhook")
async def wazzup_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        messages = data.get("messages", [])
        
        for msg in messages:
            background_tasks.add_task(process_wazzup_message, msg)
            
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error in wazzup_webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
