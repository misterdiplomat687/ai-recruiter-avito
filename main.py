from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
import httpx
import os
import json
import logging
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Environment variables
BITRIX_CLIENT_ID = os.getenv("BITRIX_CLIENT_ID", "")
BITRIX_CLIENT_SECRET = os.getenv("BITRIX_CLIENT_SECRET", "")
BITRIX_PORTAL = os.getenv("BITRIX_PORTAL", "svoya-disp.bitrix24.ru")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DISPATCHER_CHAT_ID = os.getenv("DISPATCHER_CHAT_ID", "")
WEBHOOK_URL = "https://ai-recruiter-avito.onrender.com/webhook"

# Token storage
tokens = {}
# Dialog memory
dialogs = {}

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

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
- Поблагодарите кандидата
- Скажите, что передадите информацию менеджеру
- В ПОСЛЕДНЕМ сообщении добавьте в самом конце маркер: [CANDIDATE_READY]
Если кандидат задаёт вопросы не по теме — вежливо верните к собеседованию.
Если кандидат говорит, что не заинтересован — поблагодарите и попрощайтесь."""


def parse_bitrix_form(data: dict) -> dict:
    """Parse Bitrix24 form-data with nested keys like auth[access_token]."""
    result = {}
    for key, value in data.items():
        if "[" in key:
            parts = key.replace("]", "").split("[")
            current = result
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value
        else:
            result[key] = value
    return result


async def parse_request_body(request: Request):
    """Parse request body - handles both JSON and form-data."""
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return await request.json()
        except Exception:
            pass
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        pass
    try:
        body = await request.body()
        return {"raw": body.decode("utf-8", errors="replace")}
    except Exception:
        return {}


async def refresh_tokens():
    if "refresh_token" not in tokens:
        return False
    async with httpx.AsyncClient() as c:
        resp = await c.get(
            "https://oauth.bitrix.info/oauth/token/",
            params={
                "grant_type": "refresh_token",
                "client_id": BITRIX_CLIENT_ID,
                "client_secret": BITRIX_CLIENT_SECRET,
                "refresh_token": tokens["refresh_token"]
            }
        )
        data = resp.json()
        if "access_token" in data:
            tokens["access_token"] = data["access_token"]
            tokens["refresh_token"] = data["refresh_token"]
            return True
    return False


async def bitrix_call(method, params=None):
    token = tokens.get("access_token")
    if not token:
        logger.error("No access token")
        return None
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(
            f"https://{BITRIX_PORTAL}/rest/{method}",
            params={"auth": token},
            json=params or {}
        )
        data = resp.json()
        if data.get("error") == "expired_token":
            if await refresh_tokens():
                token = tokens.get("access_token")
                resp = await c.post(
                    f"https://{BITRIX_PORTAL}/rest/{method}",
                    params={"auth": token},
                    json=params or {}
                )
                data = resp.json()
    return data


async def get_gpt_response(dialog_id, user_message):
    if dialog_id not in dialogs:
        dialogs[dialog_id] = []
    dialogs[dialog_id].append({"role": "user", "content": user_message})
    if len(dialogs[dialog_id]) > 20:
        dialogs[dialog_id] = dialogs[dialog_id][-20:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + dialogs[dialog_id]
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7
    )
    assistant_msg = response.choices[0].message.content
    dialogs[dialog_id].append({"role": "assistant", "content": assistant_msg})
    return assistant_msg


async def send_candidate_card(dialog_id, conversation):
    card_text = "[B]Новый кандидат с Авито[/B]\n\n"
    for msg in conversation:
        if msg["role"] == "user":
            card_text += f"Кандидат: {msg['content']}\n"
        elif msg["role"] == "assistant":
            card_text += f"Бот: {msg['content']}\n"
    card_text += f"\nID диалога: {dialog_id}"
    if DISPATCHER_CHAT_ID:
        await bitrix_call("im.message.add", {
            "DIALOG_ID": DISPATCHER_CHAT_ID,
            "MESSAGE": card_text
        })


async def bind_events():
    """Register event handlers after install."""
    events = [
        "OnOpenLineMessageAdd",
    ]
    for event in events:
        result = await bitrix_call("event.bind", {
            "event": event,
            "handler": WEBHOOK_URL
        })
        logger.info(f"Bind {event}: {result}")


@app.get("/")
async def root():
    return {"status": "ok", "app": "AI Recruiter Avito", "tokens": bool(tokens.get("access_token"))}


@app.get("/install")
async def install_get(request: Request):
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h1>Install: no code provided</h1>")
    async with httpx.AsyncClient() as c:
        resp = await c.get(
            "https://oauth.bitrix.info/oauth/token/",
            params={
                "grant_type": "authorization_code",
                "client_id": BITRIX_CLIENT_ID,
                "client_secret": BITRIX_CLIENT_SECRET,
                "code": code
            }
        )
        data = resp.json()
    logger.info(f"Install GET: {data}")
    if "access_token" in data:
        tokens["access_token"] = data["access_token"]
        tokens["refresh_token"] = data["refresh_token"]
        await bind_events()
        return HTMLResponse("<h1>App installed successfully!</h1>")
    return HTMLResponse(f"<h1>Install error</h1><pre>{json.dumps(data, indent=2)}</pre>")


@app.post("/install")
async def install_post(request: Request):
    raw = await parse_request_body(request)
    body = parse_bitrix_form(raw)
    logger.info(f"Install POST: {json.dumps(body, default=str, ensure_ascii=False)[:600]}")

    # Extract auth tokens — Bitrix sends auth[access_token] etc.
    auth = body.get("auth", {})
    access_token = auth.get("access_token") if isinstance(auth, dict) else None
    refresh_token = auth.get("refresh_token") if isinstance(auth, dict) else None

    # Fallback flat keys
    if not access_token:
        access_token = body.get("AUTH_ID") or body.get("auth_id")
        refresh_token = body.get("REFRESH_ID") or body.get("refresh_id")

    if access_token:
        tokens["access_token"] = access_token
        if refresh_token:
            tokens["refresh_token"] = refresh_token
        logger.info(f"Token saved: {access_token[:15]}...")
        await bind_events()
        return JSONResponse({"status": "ok"})

    return JSONResponse({"status": "received", "keys": list(raw.keys())})


@app.post("/webhook")
async def webhook(request: Request):
    try:
        raw = await parse_request_body(request)
        body = parse_bitrix_form(raw)
        logger.info(f"Webhook: {json.dumps(body, default=str, ensure_ascii=False)[:800]}")

        # Always update tokens from webhook auth if provided
        auth = body.get("auth", {})
        if isinstance(auth, dict) and auth.get("access_token"):
            tokens["access_token"] = auth["access_token"]
            if auth.get("refresh_token"):
                tokens["refresh_token"] = auth["refresh_token"]

        event = body.get("event", "").upper()
        data = body.get("data", {})

        # OnOpenLineMessageAdd — new message from client in Open Line
        if event == "ONOPENLINEMESSAGEADD":
            params = data.get("PARAMS", {})
            dialog_id = str(params.get("DIALOG_ID", ""))
            message = params.get("MESSAGE", "")
            # Skip operator messages (FROM_CONNECTOR = N means from operator)
            from_connector = params.get("FROM_CONNECTOR", "Y")
            if from_connector == "N":
                logger.info("Skipping operator message")
                return JSONResponse({"status": "skip_operator"})

            if not message or not dialog_id:
                return JSONResponse({"status": "no_data"})

            gpt_response = await get_gpt_response(dialog_id, message)

            if "[CANDIDATE_READY]" in gpt_response:
                clean = gpt_response.replace("[CANDIDATE_READY]", "").strip()
                await bitrix_call("imopenlines.message.add", {
                    "DIALOG_ID": dialog_id,
                    "MESSAGE": clean
                })
                await send_candidate_card(dialog_id, dialogs.get(dialog_id, []))
            else:
                await bitrix_call("imopenlines.message.add", {
                    "DIALOG_ID": dialog_id,
                    "MESSAGE": gpt_response
                })

            return JSONResponse({"status": "ok"})

        logger.info(f"Unknown event: {event}, keys: {list(body.keys())}")
        return JSONResponse({"status": "unknown", "event": event})

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)})


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "has_token": bool(tokens.get("access_token")),
        "dialogs_count": len(dialogs)
    }
