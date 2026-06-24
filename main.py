import os, json, hmac, hashlib, re
from datetime import date, datetime
from contextlib import asynccontextmanager

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "").strip()
API_BASE        = os.getenv("API_BASE", "https://apinet.cloud").rstrip("/")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "calorie_secret_2025")
FREE_SCAN_LIMIT = int(os.getenv("FREE_SCAN_LIMIT", "10"))
STARS_PRICE     = int(os.getenv("STARS_PRICE", "50"))
DEBUG_MODE      = os.getenv("DEBUG_MODE", "false").lower() == "true"
DATABASE_URL    = os.getenv("DATABASE_URL", "")

ANALYSIS_PROMPT = """Ты — нутрициолог-ассистент в приложении для подсчёта калорий.
Проанализируй блюдо на фотографии и оцени его пищевую ценность.

Ответь СТРОГО в виде JSON-объекта, без markdown-разметки, без текста до или после JSON:
{
  "dish_name": "название блюда на русском",
  "estimated_weight_g": число (вес порции в граммах),
  "calories": число (калорийность порции в ккал),
  "protein_g": число (белки в граммах),
  "fat_g": число (жиры в граммах),
  "carbs_g": число (углеводы в граммах),
  "confidence": "low" или "medium" или "high",
  "notes": "короткое пояснение на русском о допущениях по размеру порции"
}"""

# ── PostgreSQL ─────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       TEXT PRIMARY KEY,
            username      TEXT,
            first_name    TEXT,
            is_subscribed INTEGER DEFAULT 0,
            free_used     INTEGER DEFAULT 0,
            created_at    TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS entries (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            entry_date  DATE NOT NULL,
            entry_time  TEXT NOT NULL,
            dish_name   TEXT,
            weight_g    REAL,
            calories    REAL,
            protein_g   REAL,
            fat_g       REAL,
            carbs_g     REAL,
            confidence  TEXT,
            notes       TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, entry_date);
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("DB initialized")

# ── Telegram ───────────────────────────────────────────────────────────────────

def verify_init_data(init_data: str) -> dict | None:
    if DEBUG_MODE:
        try:
            import urllib.parse
            params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
            if "user" in params:
                return json.loads(params["user"])
        except: pass
        return {"id": 999999, "first_name": "Test", "username": "testuser"}

    if not init_data:
        return None
    try:
        import urllib.parse
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        h = params.pop("hash", "")
        dc = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        if hmac.compare_digest(hmac.new(secret, dc.encode(), hashlib.sha256).hexdigest(), h):
            return json.loads(params.get("user", "{}"))
        params2 = {}
        for part in init_data.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params2[k] = v
        h2 = params2.pop("hash", "")
        dc2 = "\n".join(f"{k}={v}" for k, v in sorted(params2.items()))
        if hmac.compare_digest(hmac.new(secret, dc2.encode(), hashlib.sha256).hexdigest(), h2):
            return json.loads(urllib.parse.unquote(params2.get("user", "{}")))
        print(f"verify failed | token: {BOT_TOKEN[:8]}...")
    except Exception as e:
        print(f"verify error: {e}")
    return None

async def send_message(chat_id, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)

# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if BOT_TOKEN and domain:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": f"https://{domain}/webhook", "secret_token": WEBHOOK_SECRET}
            )
            print("Webhook:", r.json())
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Models ─────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    init_data: str
    image_base64: str
    media_type: str = "image/jpeg"

class DiaryRequest(BaseModel):
    init_data: str
    entry: dict

# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись Telegram")

    user_id = str(user["id"])
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, username, first_name) VALUES (%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, user.get("username",""), user.get("first_name",""))
    )
    conn.commit()
    cur.execute("SELECT is_subscribed, free_used FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    is_sub = row["is_subscribed"]; free_used = row["free_used"]
    cur.close(); conn.close()

    if not is_sub and free_used >= FREE_SCAN_LIMIT:
        raise HTTPException(402, "Лимит исчерпан")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{API_BASE}/v1/messages",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ANTHROPIC_KEY}",
                "anthropic-version": "2023-06-01",
                "x-api-key": ANTHROPIC_KEY,
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": req.media_type, "data": req.image_base64}},
                    {"type": "text", "text": ANALYSIS_PROMPT}
                ]}]
            }
        )

    if resp.status_code != 200:
        print(f"API error {resp.status_code}: {resp.text[:300]}")
        raise HTTPException(500, f"Ошибка API: {resp.status_code}")

    data = resp.json()
    print(f"API response: {json.dumps(data)[:200]}")
    raw = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
    raw = raw.replace("```json","").replace("```","").strip()
    try:
        result = json.loads(raw)
    except:
        m = re.search(r'\{[\s\S]*\}', raw)
        result = json.loads(m.group()) if m else {}

    conn = get_db(); cur = conn.cursor()
    if not is_sub:
        cur.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=%s", (user_id,))
        conn.commit()
    cur.close(); conn.close()

    scans_left = max(0, FREE_SCAN_LIMIT - (free_used + 1)) if not is_sub else None
    return {"result": result, "scans_left": scans_left, "is_subscribed": bool(is_sub)}


@app.post("/diary/add")
async def diary_add(req: DiaryRequest):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    e = req.entry
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        """INSERT INTO entries
           (user_id, entry_date, entry_time, dish_name, weight_g, calories, protein_g, fat_g, carbs_g, confidence, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (str(user["id"]), date.today(), datetime.now().strftime("%H:%M"),
         e.get("dish_name"), e.get("estimated_weight_g"),
         e.get("calories"), e.get("protein_g"), e.get("fat_g"), e.get("carbs_g"),
         e.get("confidence"), e.get("notes"))
    )
    entry_id = cur.fetchone()["id"]
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "id": entry_id}


@app.get("/diary/today")
async def diary_today(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT * FROM entries WHERE user_id=%s AND entry_date=%s ORDER BY created_at",
        (str(user["id"]), date.today())
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    # Сериализуем date объекты
    for r in rows:
        for k,v in r.items():
            if hasattr(v, 'isoformat'):
                r[k] = v.isoformat()
    return {"entries": rows}


@app.get("/diary/week")
async def diary_week(init_data: str):
    """История за последние 7 дней"""
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        """SELECT entry_date, 
                  SUM(calories) as calories,
                  SUM(protein_g) as protein_g,
                  SUM(fat_g) as fat_g,
                  SUM(carbs_g) as carbs_g,
                  COUNT(*) as meals
           FROM entries
           WHERE user_id=%s AND entry_date >= CURRENT_DATE - INTERVAL '7 days'
           GROUP BY entry_date ORDER BY entry_date DESC""",
        (str(user["id"]),)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        for k,v in r.items():
            if hasattr(v,'isoformat'): r[k]=v.isoformat()
    return {"days": rows}


@app.delete("/diary/{entry_id}")
async def diary_delete(entry_id: int, request: Request):
    body = await request.json()
    user = verify_init_data(body.get("init_data",""))
    if not user:
        raise HTTPException(403, "Неверная подпись")
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM entries WHERE id=%s AND user_id=%s", (entry_id, str(user["id"])))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}


@app.get("/subscription/status")
async def sub_status(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Неверная подпись")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT is_subscribed, free_used FROM users WHERE user_id=%s", (str(user["id"]),))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return {"is_subscribed": False, "free_used": 0, "free_left": FREE_SCAN_LIMIT}
    return {
        "is_subscribed": bool(row["is_subscribed"]),
        "free_used": row["free_used"],
        "free_left": max(0, FREE_SCAN_LIMIT - row["free_used"]) if not row["is_subscribed"] else None
    }


@app.post("/subscription/buy")
async def sub_buy(request: Request):
    body = await request.json()
    user = verify_init_data(body.get("init_data",""))
    if not user:
        raise HTTPException(403, "Неверная подпись")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice",
            json={
                "chat_id": user["id"],
                "title": "Подписка КБЖУ",
                "description": "Безлимитное сканирование блюд на 30 дней",
                "payload": f"sub_{user['id']}",
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": "Подписка на месяц", "amount": STARS_PRICE}]
            }
        )
    return resp.json()


@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token","")
    if WEBHOOK_SECRET and secret and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "bad secret")
    update = await request.json()
    if msg := update.get("message"):
        text = msg.get("text","")
        chat_id = msg["chat"]["id"]
        u = msg.get("from",{})
        if text.startswith("/start"):
            domain = os.getenv("RAILWAY_PUBLIC_DOMAIN","")
            app_url = f"https://{domain}/static/index.html"
            await send_message(chat_id,
                f"Привет! 👋 Я помогу тебе следить за питанием без скучных таблиц и ручного подсчёта. "
                f"Просто фотографируй еду — остальное сделаю я 📸\n\n"
                f"У тебя <b>{FREE_SCAN_LIMIT} бесплатных сканов</b>. Поехали! 👇",
                reply_markup={"inline_keyboard": [[
                    {"text": "🥗 Открыть КБЖУ", "web_app": {"url": app_url}}
                ]]}
            )
        elif payment := msg.get("successful_payment"):
            uid = payment.get("invoice_payload","").replace("sub_","")
            conn = get_db(); cur = conn.cursor()
            cur.execute("UPDATE users SET is_subscribed=1 WHERE user_id=%s", (uid,))
            conn.commit(); cur.close(); conn.close()
            await send_message(chat_id, "🎉 <b>Подписка активирована!</b> Сканируй без ограничений 30 дней 🥗")
    elif pcq := update.get("pre_checkout_query"):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerPreCheckoutQuery",
                json={"pre_checkout_query_id": pcq["id"], "ok": True}
            )
    return {"ok": True}


@app.get("/health")
async def health():
    db_ok = False
    try:
        conn = get_db(); conn.close(); db_ok = True
    except: pass
    return {"status": "ok", "db": db_ok, "debug": DEBUG_MODE}

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")
