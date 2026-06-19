import os, json, hmac, hashlib, base64, sqlite3
from datetime import date, datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic

BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "").strip()
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "calorie_secret_2025")
FREE_SCAN_LIMIT  = int(os.getenv("FREE_SCAN_LIMIT", "5"))
STARS_PRICE      = int(os.getenv("STARS_PRICE", "150"))
DEBUG_MODE       = os.getenv("DEBUG_MODE", "false").lower() == "true"

DB_PATH = "diary.db"

# ── База данных ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      TEXT PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            is_subscribed INTEGER DEFAULT 0,
            free_used    INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS entries (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            time         TEXT NOT NULL,
            dish_name    TEXT,
            weight_g     REAL,
            calories     REAL,
            protein_g    REAL,
            fat_g        REAL,
            carbs_g      REAL,
            confidence   TEXT,
            notes        TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

# ── Telegram helpers ───────────────────────────────────────────────────────────

def verify_init_data(init_data: str) -> dict | None:
    """Проверяем подпись initData от Telegram WebApp."""
    # DEBUG режим — пропускаем проверку, возвращаем тестового пользователя
    if DEBUG_MODE:
        print("DEBUG_MODE: skipping verification")
        try:
            import urllib.parse
            params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
            if "user" in params:
                return json.loads(params["user"])
        except:
            pass
        return {"id": 999999, "first_name": "Test", "username": "testuser"}

    if not init_data:
        return None

    import urllib.parse

    try:
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()

        # Метод 1: parse_qsl (декодирует URL-encoded значения)
        params1 = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        h1 = params1.pop("hash", "")
        dc1 = "\n".join(f"{k}={v}" for k, v in sorted(params1.items()))
        c1 = hmac.new(secret, dc1.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(c1, h1):
            print("verify OK (method 1)")
            return json.loads(params1.get("user", "{}"))

        # Метод 2: raw split без декодирования
        params2 = {}
        for part in init_data.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params2[k] = v
        h2 = params2.pop("hash", "")
        dc2 = "\n".join(f"{k}={v}" for k, v in sorted(params2.items()))
        c2 = hmac.new(secret, dc2.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(c2, h2):
            print("verify OK (method 2)")
            return json.loads(urllib.parse.unquote(params2.get("user", "{}")))

        print(f"Both methods failed | m1:{c1[:8]} m2:{c2[:8]} got:{h1[:8]} | token_start:{BOT_TOKEN[:6]}")
    except Exception as e:
        print(f"verify error: {e}")
    return None

async def send_message(chat_id: int | str, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)

async def answer_pre_checkout(query_id: str, ok: bool, error: str = ""):
    async with httpx.AsyncClient() as client:
        payload = {"pre_checkout_query_id": query_id, "ok": ok}
        if not ok:
            payload["error_message"] = error
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerPreCheckoutQuery", json=payload)

# ── Lifespan: старт приложения ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if BOT_TOKEN and domain:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": f"https://{domain}/webhook", "secret_token": WEBHOOK_SECRET}
            )
            print("Webhook set:", resp.json())
    yield

# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Pydantic модели ────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    init_data: str
    image_base64: str
    media_type: str = "image/jpeg"

class DiaryRequest(BaseModel):
    init_data: str
    entry: dict

class DeleteRequest(BaseModel):
    init_data: str
    entry_id: int

# ── Эндпоинты ─────────────────────────────────────────────────────────────────

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись Telegram")

    user_id = str(user["id"])
    conn = get_db()

    # Создаём пользователя если нет
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
        (user_id, user.get("username",""), user.get("first_name",""))
    )
    conn.commit()

    row = conn.execute("SELECT is_subscribed, free_used FROM users WHERE user_id=?", (user_id,)).fetchone()
    is_subscribed = row["is_subscribed"]
    free_used     = row["free_used"]

    if not is_subscribed and free_used >= FREE_SCAN_LIMIT:
        conn.close()
        raise HTTPException(402, f"Бесплатный лимит исчерпан ({FREE_SCAN_LIMIT} сканов). Оформи подписку.")

    # Вызываем Claude
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url="https://apinet.cloud")
    prompt = """Ты — нутрициолог-ассистент. Проанализируй блюдо на фото и оцени пищевую ценность.
Ответь СТРОГО в виде JSON (без markdown, без текста до/после):
{
  "dish_name": "название на русском",
  "estimated_weight_g": число,
  "calories": число,
  "protein_g": число,
  "fat_g": число,
  "carbs_g": число,
  "confidence": "low"|"medium"|"high",
  "notes": "пояснение на русском"
}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": req.media_type, "data": req.image_base64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )

    raw = message.content[0].text.strip().replace("```json","").replace("```","")
    try:
        result = json.loads(raw)
    except:
        import re
        m = re.search(r'\{[\s\S]*\}', raw)
        result = json.loads(m.group()) if m else {}

    # Обновляем счётчик бесплатных сканов
    if not is_subscribed:
        conn.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=?", (user_id,))
        conn.commit()

    scans_left = max(0, FREE_SCAN_LIMIT - (free_used + 1)) if not is_subscribed else None
    conn.close()

    return {"result": result, "scans_left": scans_left, "is_subscribed": bool(is_subscribed)}


@app.post("/diary/add")
async def diary_add(req: DiaryRequest):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    user_id = str(user["id"])
    e = req.entry
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO entries (user_id,date,time,dish_name,weight_g,calories,protein_g,fat_g,carbs_g,confidence,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, date.today().isoformat(),
         datetime.now().strftime("%H:%M"),
         e.get("dish_name"), e.get("estimated_weight_g"),
         e.get("calories"), e.get("protein_g"), e.get("fat_g"), e.get("carbs_g"),
         e.get("confidence"), e.get("notes"))
    )
    entry_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": entry_id}


@app.get("/diary/today")
async def diary_today(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    user_id = str(user["id"])
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id=? AND date=? ORDER BY created_at",
        (user_id, date.today().isoformat())
    ).fetchall()
    conn.close()
    return {"entries": [dict(r) for r in rows]}


@app.delete("/diary/{entry_id}")
async def diary_delete(entry_id: int, request: Request):
    body = await request.json()
    user = verify_init_data(body.get("init_data",""))
    if not user:
        raise HTTPException(403, "Неверная подпись")
    conn = get_db()
    conn.execute("DELETE FROM entries WHERE id=? AND user_id=?", (entry_id, str(user["id"])))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/subscription/status")
async def sub_status(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    conn = get_db()
    row = conn.execute("SELECT is_subscribed, free_used FROM users WHERE user_id=?", (str(user["id"]),)).fetchone()
    conn.close()
    if not row:
        return {"is_subscribed": False, "free_used": 0, "free_left": FREE_SCAN_LIMIT}
    return {
        "is_subscribed": bool(row["is_subscribed"]),
        "free_used": row["free_used"],
        "free_left": max(0, FREE_SCAN_LIMIT - row["free_used"]) if not row["is_subscribed"] else None
    }


@app.post("/subscription/buy")
async def sub_buy(request: Request):
    """Отправляет инвойс на оплату через Telegram Stars."""
    body = await request.json()
    user = verify_init_data(body.get("init_data",""))
    if not user:
        raise HTTPException(403, "Неверная подпись")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice",
            json={
                "chat_id": user["id"],
                "title": "Подписка CalorieAI",
                "description": "Безлимитное сканирование блюд на 30 дней",
                "payload": f"sub_{user['id']}",
                "provider_token": "",          # пусто = Telegram Stars
                "currency": "XTR",
                "prices": [{"label": "Подписка на месяц", "amount": STARS_PRICE}]
            }
        )
    return resp.json()


# ── Telegram webhook ───────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if WEBHOOK_SECRET and secret and secret != WEBHOOK_SECRET:
        print(f"Webhook secret mismatch: got '{secret}', expected '{WEBHOOK_SECRET}'")
        raise HTTPException(403, "bad secret")

    update = await request.json()
    print("Update:", json.dumps(update)[:300])

    # /start
    if msg := update.get("message"):
        text = msg.get("text","")
        chat_id = msg["chat"]["id"]
        user = msg.get("from",{})

        if text.startswith("/start"):
            domain = os.getenv("RAILWAY_PUBLIC_DOMAIN","")
            app_url = f"https://{domain}/static/index.html"
            await send_message(chat_id,
                f"👋 Привет, <b>{user.get('first_name','друг')}</b>!\n\n"
                "🍽 <b>CalorieAI</b> — умный дневник питания.\n"
                "Сфотографируй блюдо — ИИ посчитает калории и БЖУ за секунды.\n\n"
                f"У тебя <b>{FREE_SCAN_LIMIT} бесплатных сканов</b>. Поехали! 👇",
                reply_markup={"inline_keyboard": [[
                    {"text": "🥗 Открыть приложение", "web_app": {"url": app_url}}
                ]]}
            )

        # Успешная оплата
        elif payment := msg.get("successful_payment"):
            payload = payment.get("invoice_payload","")
            if payload.startswith("sub_"):
                uid = payload.split("_")[1]
                conn = get_db()
                conn.execute("UPDATE users SET is_subscribed=1 WHERE user_id=?", (uid,))
                conn.commit()
                conn.close()
                await send_message(chat_id,
                    "🎉 <b>Подписка активирована!</b>\n"
                    "Теперь сканируй блюда без ограничений 30 дней. Приятного аппетита! 🥗"
                )

    # Pre-checkout (обязательно подтверждать)
    elif pcq := update.get("pre_checkout_query"):
        await answer_pre_checkout(pcq["id"], ok=True)

    return {"ok": True}


# ── Статика ───────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/health")
async def health():
    return {"status": "ok"}
