import os, json, hmac, hashlib, re
from datetime import date, datetime
from contextlib import asynccontextmanager

import httpx, asyncpg
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
ADMIN_IDS = set(x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
STARS_PRICE     = int(os.getenv("STARS_PRICE", "50"))
DEBUG_MODE      = os.getenv("DEBUG_MODE", "false").lower() == "true"
DATABASE_URL    = os.getenv("DATABASE_URL", "")

ANALYSIS_PROMPT = """Ты — нутрициолог-ассистент. Проанализируй блюдо на фотографии.
Ответь СТРОГО в виде JSON без markdown:
{"dish_name":"название на русском","estimated_weight_g":число,"calories":число,"protein_g":число,"fat_g":число,"carbs_g":число,"confidence":"low|medium|high","notes":"пояснение"}"""

pool = None

# ── DB ─────────────────────────────────────────────────────────────────────────

async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return pool

async def init_db():
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
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
    print("DB initialized OK")

# ── Telegram ───────────────────────────────────────────────────────────────────

def verify_init_data(init_data: str) -> dict | None:
    """Извлекаем реального пользователя из initData Telegram."""
    if not init_data:
        return None
    import urllib.parse
    # Метод 1: parse_qsl
    try:
        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        user_str = params.get("user", "")
        if user_str:
            user = json.loads(user_str)
            if user.get("id"):
                return user
    except Exception as e:
        print(f"parse_qsl error: {e}")
    # Метод 2: raw split + unquote
    try:
        params2 = {}
        for part in init_data.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params2[k] = v
        user_str = urllib.parse.unquote(params2.get("user", ""))
        if user_str:
            user = json.loads(user_str)
            if user.get("id"):
                return user
    except Exception as e:
        print(f"raw split error: {e}")
    print("Could not extract user from initData")
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
    await init_db()
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if BOT_TOKEN and domain:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": f"https://{domain}/webhook", "secret_token": WEBHOOK_SECRET}
            )
            print("Webhook:", r.json())
    yield
    if pool:
        await pool.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
        raise HTTPException(403, "Неверная подпись")

    user_id = str(user["id"])
    p = await get_pool()

    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES ($1,$2,$3) ON CONFLICT (user_id) DO NOTHING",
            user_id, user.get("username",""), user.get("first_name","")
        )
        row = await conn.fetchrow("SELECT is_subscribed, free_used FROM users WHERE user_id=$1", user_id)
        is_sub = row["is_subscribed"]; free_used = row["free_used"]

        if not is_sub and free_used >= FREE_SCAN_LIMIT:
            raise HTTPException(402, "Лимит исчерпан")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{API_BASE}/v1/messages",
            headers={"Content-Type":"application/json","Authorization":f"Bearer {ANTHROPIC_KEY}","anthropic-version":"2023-06-01","x-api-key":ANTHROPIC_KEY},
            json={"model":"claude-sonnet-4-6","max_tokens":1000,"messages":[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":req.media_type,"data":req.image_base64}},
                {"type":"text","text":ANALYSIS_PROMPT}
            ]}]}
        )

    if resp.status_code != 200:
        print(f"API error {resp.status_code}: {resp.text[:200]}")
        raise HTTPException(500, f"Ошибка API")

    data = resp.json()
    raw = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
    raw = raw.replace("```json","").replace("```","").strip()
    try:
        result = json.loads(raw)
    except:
        m = re.search(r'\{[\s\S]*\}', raw)
        result = json.loads(m.group()) if m else {}

    async with p.acquire() as conn:
        if not is_sub and not is_admin:
            await conn.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=$1", user_id)

    scans_left = None if (is_sub or is_admin) else max(0, FREE_SCAN_LIMIT - (free_used + 1))
    return {"result": result, "scans_left": scans_left, "is_subscribed": bool(is_sub)}


@app.post("/diary/add")
async def diary_add(req: DiaryRequest):
    user = verify_init_data(req.init_data)
    if not user: raise HTTPException(403, "Неверная подпись")
    e = req.entry
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO entries (user_id,entry_date,entry_time,dish_name,weight_g,calories,protein_g,fat_g,carbs_g,confidence,notes) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id",
            str(user["id"]), date.today(), datetime.now().strftime("%H:%M"),
            e.get("dish_name"), e.get("estimated_weight_g"),
            e.get("calories"), e.get("protein_g"), e.get("fat_g"), e.get("carbs_g"),
            e.get("confidence"), e.get("notes")
        )
    return {"ok": True, "id": row["id"]}


@app.get("/diary/today")
async def diary_today(init_data: str):
    user = verify_init_data(init_data)
    if not user: raise HTTPException(403, "Неверная подпись")
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM entries WHERE user_id=$1 AND entry_date=$2 ORDER BY created_at",
            str(user["id"]), date.today()
        )
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, 'isoformat'): d[k] = v.isoformat()
        result.append(d)
    return {"entries": result}


@app.get("/diary/week")
async def diary_week(init_data: str):
    user = verify_init_data(init_data)
    if not user: raise HTTPException(403, "Неверная подпись")
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """SELECT entry_date, SUM(calories) as calories, SUM(protein_g) as protein_g,
               SUM(fat_g) as fat_g, SUM(carbs_g) as carbs_g, COUNT(*) as meals
               FROM entries WHERE user_id=$1 AND entry_date >= CURRENT_DATE - INTERVAL '7 days'
               GROUP BY entry_date ORDER BY entry_date DESC""",
            str(user["id"])
        )
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, 'isoformat'): d[k] = v.isoformat()
        result.append(d)
    return {"days": result}


@app.delete("/diary/{entry_id}")
async def diary_delete(entry_id: int, request: Request):
    body = await request.json()
    user = verify_init_data(body.get("init_data",""))
    if not user: raise HTTPException(403, "Неверная подпись")
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM entries WHERE id=$1 AND user_id=$2", entry_id, str(user["id"]))
    return {"ok": True}


@app.get("/subscription/status")
async def sub_status(init_data: str):
    user = verify_init_data(init_data)
    if not user: raise HTTPException(403, "Неверная подпись")
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT is_subscribed, free_used FROM users WHERE user_id=$1", str(user["id"]))
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
    if not user: raise HTTPException(403, "Неверная подпись")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice", json={
            "chat_id": user["id"], "title": "Подписка КБЖУ",
            "description": "Безлимитное сканирование блюд на 30 дней",
            "payload": f"sub_{user['id']}", "provider_token": "", "currency": "XTR",
            "prices": [{"label": "Подписка на месяц", "amount": STARS_PRICE}]
        })
    return resp.json()


@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token","")
    if WEBHOOK_SECRET and secret and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "bad secret")
    update = await request.json()
    if msg := update.get("message"):
        text = msg.get("text",""); chat_id = msg["chat"]["id"]; u = msg.get("from",{})
        if text.startswith("/start"):
            domain = os.getenv("RAILWAY_PUBLIC_DOMAIN","")
            await send_message(chat_id,
                f"Привет, <b>{u.get('first_name','друг')}</b>! 👋\n\nЯ помогу следить за питанием без скучных таблиц. Просто фотографируй еду — остальное сделаю я 📸\n\nУ тебя <b>{FREE_SCAN_LIMIT} бесплатных сканов</b>. Поехали! 👇",
                reply_markup={"inline_keyboard": [[{"text":"🥗 Открыть КБЖУ","web_app":{"url":f"https://{domain}/static/index.html"}}]]}
            )
        elif payment := msg.get("successful_payment"):
            uid = payment.get("invoice_payload","").replace("sub_","")
            p = await get_pool()
            async with p.acquire() as conn:
                await conn.execute("UPDATE users SET is_subscribed=1 WHERE user_id=$1", uid)
            await send_message(chat_id, "🎉 <b>Подписка активирована!</b> Сканируй без ограничений 30 дней 🥗")
    elif pcq := update.get("pre_checkout_query"):
        async with httpx.AsyncClient() as client:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerPreCheckoutQuery",
                json={"pre_checkout_query_id": pcq["id"], "ok": True})
    return {"ok": True}


@app.get("/health")
async def health():
    db_ok = False
    try:
        p = await get_pool()
        async with p.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except: pass
    return {"status": "ok", "db": db_ok, "debug": DEBUG_MODE}


PRIVACY_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Политика конфиденциальности — КБЖУ</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b2a38;color:#dff0f7;padding:24px 16px 48px;max-width:640px;margin:0 auto;line-height:1.7}
h1{font-size:22px;font-weight:800;margin-bottom:8px;color:#fff}
h2{font-size:16px;font-weight:700;margin:24px 0 8px;color:#dff0f7}
p{font-size:14px;color:#b0cfe0;margin-bottom:12px}
ul{font-size:14px;color:#b0cfe0;margin-bottom:12px;padding-left:20px}
li{margin-bottom:6px}
.date{font-size:12px;color:#7cb8d0;margin-bottom:32px}
a{color:#5ba3c9;text-decoration:none}
</style>
</head>
<body>
<h1>Политика конфиденциальности</h1>
<div class="date">Дата последнего обновления: 24 июня 2026 г.</div>
<p>Настоящая Политика конфиденциальности описывает, как бот КБЖУ собирает, использует и защищает данные пользователей.</p>
<h2>1. Какие данные мы собираем</h2>
<ul>
<li>Идентификатор пользователя Telegram (user_id)</li>
<li>Имя пользователя и никнейм в Telegram (если доступны)</li>
<li>Фотографии блюд, которые вы отправляете для анализа</li>
<li>Данные о питании: названия блюд, вес порций, калории, БЖУ</li>
<li>Дата и время добавления записей в дневник</li>
</ul>
<h2>2. Как мы используем данные</h2>
<ul>
<li>Для анализа фотографий блюд с помощью ИИ и расчёта КБЖУ</li>
<li>Для ведения персонального дневника питания</li>
<li>Для отображения статистики и прогресса</li>
<li>Для проверки статуса подписки и учёта бесплатных сканов</li>
</ul>
<h2>3. Хранение данных</h2>
<p>Данные дневника питания хранятся в защищённой базе данных. Фотографии блюд передаются на сервер ИИ для анализа и не сохраняются постоянно. Мы храним только текстовый результат анализа.</p>
<h2>4. Передача данных третьим лицам</h2>
<p>Фотографии блюд передаются сервису искусственного интеллекта исключительно для распознавания и расчёта пищевой ценности. Мы не продаём и не передаём персональные данные третьим лицам в коммерческих целях.</p>
<h2>5. Платежи</h2>
<p>Оплата подписки осуществляется через встроенную систему Telegram Stars. Мы не получаем и не храним данные банковских карт пользователей.</p>
<h2>6. Удаление данных</h2>
<p>Вы можете удалить свои записи в любое время через интерфейс приложения. Для полного удаления аккаунта напишите в поддержку.</p>
<h2>7. Возраст пользователей</h2>
<p>Сервис предназначен для лиц старше 16 лет.</p>
<h2>8. Контакты</h2>
<p>По вопросам конфиденциальности: <a href="https://t.me/КБЖУ">написать в бот</a></p>
</body>
</html>"""

@app.get("/privacy")
async def privacy():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=PRIVACY_HTML)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")
