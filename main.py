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

# Token storage (in-memory, use DB in production)
tokens = {}
# Dialog memory
dialogs = {}

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """You are an AI recruiter for the company 'Svoya Dispetcherskaya'. Your task is to conduct a preliminary interview with candidates who responded to a job posting on Avito.
Rules:
- Communicate in Russian only
- Be friendly and professional
- Ask questions ONE AT A TIME, wait for answer before next question
- Questions to ask in order:
  1. What is your name?
  2. How old are you?
  3. Where do you live (city)?
  4. Do you have experience as a dispatcher/operator?
  5. Are you ready to work in shifts (day/night)?
  6. When can you start?
After all questions are answered:
- Thank the candidate
- Say you will pass the information to the manager
- In your LAST message, add a special marker at the very end: [CANDIDATE_READY]
If the candidate asks off-topic questions, politely redirect to the interview.
If the candidate says they are not interested, thank them and say goodbye."""


async def get_access_token():
    if "access_token" in tokens:
        return tokens["access_token"]
    return None


async def refresh_tokens():
    if "refresh_token" not in tokens:
        return False
    async with httpx.AsyncClient() as client_http:
        resp = await client_http.get(
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
    token = await get_access_token()
    if not token:
        logger.error("No access token available")
        return None
    async with httpx.AsyncClient() as client_http:
        resp = await client_http.post(
            f"https://{BITRIX_PORTAL}/rest/{method}",
            params={"auth": token},
            json=params or {}
        )
        data = resp.json()
        if "error" in data and data["error"] == "expired_token":
            if await refresh_tokens():
                token = await get_access_token()
                resp = await client_http.post(
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
    # Keep last 20 messages
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


async def parse_request_body(request: Request):
    """Parse request body - handles both JSON and form-data."""
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return await request.json()
        except Exception:
            pass
    if "form" in content_type or "urlencoded" in content_type:
        form = await request.form()
        return dict(form)
    # Try JSON first, then form
    try:
        return await request.json()
    except Exception:
        try:
            form = await request.form()
            return dict(form)
        except Exception:
            body = await request.body()
            return {"raw": body.decode("utf-8", errors="replace")}


@app.get("/")
async def root():
    return {"status": "ok", "app": "AI Recruiter Avito"}


@app.get("/install")
async def install_get(request: Request):
    code = request.query_params.get("code")
    domain = request.query_params.get("domain")
    if not code:
        return HTMLResponse("<h1>Install: no code provided</h1>")
    async with httpx.AsyncClient() as client_http:
        resp = await client_http.get(
            "https://oauth.bitrix.info/oauth/token/",
            params={
                "grant_type": "authorization_code",
                "client_id": BITRIX_CLIENT_ID,
                "client_secret": BITRIX_CLIENT_SECRET,
                "code": code
            }
        )
        data = resp.json()
        logger.info(f"Install GET response: {data}")
    if "access_token" in data:
        tokens["access_token"] = data["access_token"]
        tokens["refresh_token"] = data["refresh_token"]
        # Register event handler for open lines messages
        result = await bitrix_call("event.bind", {
            "event": "ONIMBOTMESSAGEADD",
            "handler": f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'ai-recruiter-avito.onrender.com')}/webhook"
        })
        logger.info(f"Event bind result: {result}")
        return HTMLResponse("<h1>App installed successfully!</h1>")
    return HTMLResponse(f"<h1>Install error</h1><pre>{json.dumps(data, indent=2)}</pre>")


@app.post("/install")
async def install_post(request: Request):
    body = await parse_request_body(request)
    logger.info(f"Install POST body: {json.dumps(body, default=str, ensure_ascii=False)[:500]}")
    # Bitrix sends AUTH_ID in form-data during install
    auth_id = body.get("AUTH_ID") or body.get("auth_id")
    refresh_id = body.get("REFRESH_ID") or body.get("refresh_id")
    if auth_id:
        tokens["access_token"] = auth_id
        if refresh_id:
            tokens["refresh_token"] = refresh_id
        logger.info(f"Tokens saved from install POST. Auth: {auth_id[:10]}...")
        # Register event handler
        result = await bitrix_call("event.bind", {
            "event": "ONIMBOTMESSAGEADD",
            "handler": f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'ai-recruiter-avito.onrender.com')}/webhook"
        })
        logger.info(f"Event bind result: {result}")
        return JSONResponse({"status": "ok"})
    # Try code-based auth
    code = body.get("code")
    if code:
        async with httpx.AsyncClient() as client_http:
            resp = await client_http.get(
                "https://oauth.bitrix.info/oauth/token/",
                params={
                    "grant_type": "authorization_code",
                    "client_id": BITRIX_CLIENT_ID,
                    "client_secret": BITRIX_CLIENT_SECRET,
                    "code": code
                }
            )
            data = resp.json()
            if "access_token" in data:
                tokens["access_token"] = data["access_token"]
                tokens["refresh_token"] = data["refresh_token"]
                return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "received", "body_keys": list(body.keys())})


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await parse_request_body(request)
        logger.info(f"Webhook received: {json.dumps(body, default=str, ensure_ascii=False)[:500]}")

        # Handle event verification
        if body.get("event") == "ONIMBOTMESSAGEADD" or "data" in body:
            data = body.get("data", body)
            params = data.get("PARAMS", data)
            dialog_id = str(params.get("DIALOG_ID", params.get("dialog_id", "")))
            message = params.get("MESSAGE", params.get("message", ""))
            from_user = params.get("FROM_USER_ID", params.get("from_user_id", ""))

            if not message or not dialog_id:
                return JSONResponse({"status": "no message"})

            # Get GPT response
            gpt_response = await get_gpt_response(dialog_id, message)

            # Check if candidate is ready
            if "[CANDIDATE_READY]" in gpt_response:
                clean_response = gpt_response.replace("[CANDIDATE_READY]", "").strip()
                await bitrix_call("im.message.add", {
                    "DIALOG_ID": dialog_id,
                    "MESSAGE": clean_response
                })
                await send_candidate_card(dialog_id, dialogs.get(dialog_id, []))
            else:
                await bitrix_call("im.message.add", {
                    "DIALOG_ID": dialog_id,
                    "MESSAGE": gpt_response
                })

            return JSONResponse({"status": "ok"})

        return JSONResponse({"status": "unknown event", "body": str(body)[:200]})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse({"status": "error", "message": str(e)})


@app.get("/health")
async def health():
    return {"status": "healthy", "tokens": bool(tokens.get("access_token"))}
