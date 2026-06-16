# CalorieAI — Telegram Mini App

Умный дневник питания с анализом фото через ИИ.

## Переменные окружения (задаются в Railway)

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от @BotFather |
| `ANTHROPIC_API_KEY` | Ключ Claude API |
| `WEBHOOK_SECRET` | Любая случайная строка (например: `my_secret_123`) |
| `FREE_SCAN_LIMIT` | Кол-во бесплатных сканов (по умолчанию 5) |
| `STARS_PRICE` | Цена подписки в Telegram Stars (по умолчанию 150) |

## Деплой на Railway

1. Загрузи все файлы в GitHub репозиторий
2. Зайди на railway.app → New Project → Deploy from GitHub
3. Выбери репозиторий
4. В разделе Variables добавь все переменные выше
5. Railway автоматически развернёт приложение
6. Скопируй публичный домен (например: `calorie-bot.up.railway.app`)
7. В BotFather: /mybots → твой бот → Menu Button → URL → вставь домен + `/static/index.html`
