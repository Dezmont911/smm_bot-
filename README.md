# SMM Bot — Content Factory

Telegram-бот для автоматической генерации и публикации постов в каналах через Anthropic Claude API.
Поддерживает информационные каналы (RSS) и маркетплейс-каналы (Wildberries).

> **Последнее обновление README:** 2026-05-28
> **Статус:** Работает на VPS `77.233.215.77` (Amsterdam)
> **Python:** 3.12, venv в `/opt/smm_bot/venv`

---

## Оглавление

- [Что делает](#что-делает)
- [Архитектура](#архитектура)
- [Файлы проекта](#файлы-проекта)
- [Команды бота](#команды-бота)
- [Маркетплейс-каналы (WB)](#маркетплейс-каналы-wb)
- [Деплой на VPS](#деплой-на-vps)
- [Переменные окружения](#переменные-окружения)
- [Карточка канала](#карточка-канала)
- [Буфер постов](#буфер-постов)
- [Публикация постов](#публикация-постов)
- [Известные технические детали](#известные-технические-детали)

---

## Что делает

1. **Генерирует посты** через Claude по теме канала — из RSS-новостей, вечнозелёных тем или товаров WB
2. **Ведёт буфер** — хранит готовые посты в очереди (минимум 8 на канал)
3. **Публикует по расписанию** — постер берёт следующий пост из очереди (09:00, 12:00, 16:00, 20:00 МСК; настраивается)
4. **Перекрывает рекламу РСЯ** — детектирует рекламные посты Яндекс и публикует свой пост через 5–15 мин
5. **Не повторяет темы** — дедупликация через последние 20 опубликованных тем
6. **Ищет картинки** — Pexels API и Unsplash по теме поста
7. **Управляется через Telegram** — добавление каналов, очередь, ручная публикация, превью

---

## Архитектура

```
RSS-ленты / WB API
       |
 rss_parser.py            wb_parser.py
 web_scraper.py           wb_partner_parser.py
       |                        |
       +------ content_generator.py <--- ai_client.py (Claude)
                    |                       ^
              image_fetcher.py       used_topics (дедупликация)
              Pexels / Unsplash
                    |
              buffer_manager.py --> SQLite (posts: status=ready)
                    |
              poster.py --> Telegram каналы
                    ^
              bot.py (управление: /add, /generate, /review, /schedule ...)
                    ^
              channel_post handler --> детектор рекламы РСЯ
```

**Поток данных (информационный канал):**
1. `content_generator` собирает темы из RSS + вечнозелёных тем
2. Загружает последние 20 тем из БД — передаёт в Claude для дедупликации
3. Для каждой темы ищет картинку через `image_fetcher`
4. Генерирует посты через Claude — кладёт в БД со статусом `ready`
5. `poster` каждый час проверяет расписание и публикует следующий `ready` пост

**Поток данных (маркетплейс-канал WB):**
1. Артикулы берутся из кеша `cards/wb_ids_cache.json`
2. Запрос к `card.wb.ru/cards/v4/detail` батчами по 20 артикулов
3. Из ответа формируется пост: бренд, название, цена, скидка, рейтинг, ссылка
4. Картинка — с CDN `wbbasket.ru` по вычисленному номеру корзины
5. Пост поступает в буфер — публикуется по расписанию

---

## Файлы проекта

| Файл | Назначение |
|------|-----------|
| `config.py` | Загрузка всех переменных из `.env`; доступ через `cfg.FIELD` |
| `database.py` | SQLite БД: channels, posts, processed_ads, evergreen_topics, error_log |
| `ai_client.py` | Генерация постов через Claude API; ротация форматов; дедупликация |
| `buffer_manager.py` | Очередь постов: add / get_next / mark_published; уровни OK/Low/Emergency/Critical |
| `rss_parser.py` | Парсинг RSS, извлечение картинок из HTML, scoring по свежести/релевантности |
| `content_generator.py` | Дирижёр: RSS → темы → Claude → буфер; утренняя и экстренная генерация |
| `image_fetcher.py` | Поиск картинок Pexels → Unsplash; перевод RU→EN через Claude |
| `web_scraper.py` | Автоподбор Reddit/Medium RSS через Claude, если основной RSS пуст |
| `wb_parser.py` | Парсер товаров WB: кеш артикулов → card.wb.ru v4 API → CDN картинки |
| `wb_partner_parser.py` | Парсер через WB Seller API (собственные товары продавца) |
| `channel_analyzer.py` | Анализ экспорта канала через Claude для автонастройки параметров |
| `poster.py` | Публикация по расписанию; 4 попытки (markdown/plain × с картинкой/без) |
| `bot.py` | Telegram бот администратора; все команды; ConversationHandler для /add |
| `ssh_test.py` | Утилита SSH-диагностики VPS через paramiko; тесты API, CDN, деплой |
| `cards/wb_ids_cache.json` | Кеш артикулов WB по категориям (280 шт., обновлять раз в 1–2 нед.) |
| `channels/*.json` | Карточки каналов (создаются через /add) |

---

## Команды бота

| Команда | Что делает |
|---------|-----------|
| `/start` | Приветствие и список команд |
| `/list` | Все каналы с состоянием буфера и кнопками управления |
| `/add` | Добавить новый канал (пошаговый диалог или импорт JSON) |
| `/status` | Уровень буфера по всем каналам |
| `/generate` | Запустить генерацию постов вручную |
| `/review` | Посмотреть очередь; редактировать текст/картинку; удалять; пагинация по 5 |
| `/preview` | Сгенерировать пост с кнопками: в очередь / опубликовать сейчас / перегенерировать |
| `/post_now` | Опубликовать следующий пост из буфера немедленно |
| `/schedule` | Управление расписанием |
| `/delete_posts` | Удалить все ready-посты канала (с подтверждением) |
| `/wb_refresh` | Инструкция по обновлению кеша артикулов WB |
| `/cancel` | Отменить текущий диалог |

### /schedule подробно

```
/schedule                        -- расписание всех каналов
/schedule @channel               -- расписание конкретного канала
/schedule @channel 09 12 16 20   -- установить часы публикаций (МСК)
/schedule @channel off           -- режим "только РСЯ" (без таймера)
/schedule @channel on            -- вернуть расписание
```

---

## Маркетплейс-каналы (WB)

### Как работает wb_parser.py

1. Артикулы берутся из кеша `cards/wb_ids_cache.json` (7 категорий × 40 артикулов = 280 шт.)
2. По артикулам запрашивается `card.wb.ru/cards/v4/detail` батчами по 20
3. Из ответа формируется пост с ценой, скидкой, рейтингом, ссылкой
4. URL картинки строится по формуле CDN корзины
5. При задании `wb_categories` в карточке — выбираются нужные категории из кеша

### Кеш артикулов — как обновить

Раз в 1–2 недели заходим на wildberries.ru, открываем категорию, в DevTools Console (F12):

```javascript
// Собрать артикулы с текущей страницы (40 шт.)
[...document.querySelectorAll('[data-nm-id]')]
  .map(el => parseInt(el.dataset.nmId))
  .filter(Boolean)
```

Вставить полученные ID в нужную категорию в `cards/wb_ids_cache.json`.

### Категории в кеше

`кроссовки`, `косметика`, `наушники беспроводные`, `сумка женская`, `термокружка`, `платье женское`, `настольные игры`

### WB API — критические технические детали

**Endpoint:**
- УСТАРЕЛ: `/cards/v2/detail` — возвращает 404 с ~2025 года
- АКТУАЛЕН: `/cards/v4/detail` (проверено 2026-05)
- Структура ответа v4: `{"products": [...]}` (в v2 было `{"data": {"products": [...]}}`)

**CDN корзины (basket) — формула:**

```
vol = article_id // 100000
part = article_id // 1000
URL = https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{article}/images/big/1.webp

Baskets 1-19: фиксированная таблица (см. wb_parser.py)
Baskets 20+: кусочно-линейная формула, проверено 2026-05:
  - (vol=4622, basket=26), (vol=7620, basket=35), (vol=7901, basket=36)
  - (vol=9017, basket=39), (vol=10243, basket=41)

if   vol <= 4622:  basket = 20 + (vol - 3270) // 225
elif vol <= 7620:  basket = 26 + (vol - 4622) // 333
elif vol <  7901:  basket = 35
elif vol <= 9017:  basket = 36 + (vol - 7901) // 372
elif vol <= 10243: basket = 39 + (vol - 9017) // 613
else:              basket = 41 + (vol - 10243) // 613
```

Таблица может устареть — проверять раз в несколько месяцев.

**Прокси:**
- С VPS Amsterdam (77.233.215.77) карточный API доступен без прокси
- `search.wb.ru` заблокирован с дата-центровых IP — не использовать
- Webshare резидентные прокси: `ryryrvfb` / `95xgdgxzev5d` (10 штук, настроены в .env)

---

## Деплой на VPS

### Текущий сервер

```
IP:      77.233.215.77
Регион:  Amsterdam, Netherlands
SSH:     ssh -i C:\Users\Admin\.ssh\smm_bot root@77.233.215.77
Проект:  /opt/smm_bot
Venv:    /opt/smm_bot/venv/bin/python
Сервис:  smm_bot.service
```

### Требования

- Ubuntu 22.04+
- **Регион НЕ Россия** — Anthropic API блокирует российские IP (HTTP 403)
- Минимум 1 vCPU, 512 MB RAM

### Первоначальный деплой (новый сервер)

```bash
git clone https://github.com/Dezmont911/smm_bot-.git /opt/smm_bot
cd /opt/smm_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

# Тест
python bot.py   # Ctrl+C если ок

# systemd
cat > /etc/systemd/system/smm_bot.service << 'EOF'
[Unit]
Description=SMM Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/smm_bot
ExecStart=/opt/smm_bot/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload && systemctl enable smm_bot && systemctl start smm_bot
```

### Обновление кода

```bash
# Windows: git add . && git commit -F msg.txt && git push
# Сервер:
cd /opt/smm_bot && git pull origin main && systemctl restart smm_bot
```

### Управление сервисом

```bash
systemctl status smm_bot
journalctl -u smm_bot -n 50 -f        # логи в реальном времени
/opt/smm_bot/venv/bin/python wb_parser.py   # тест WB парсера
```

### SSH через Python (ssh_test.py)

Файл `ssh_test.py` использует `paramiko` — надёжнее чем `ssh.exe` в Windows:

```python
import paramiko, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("77.233.215.77", port=22, username="root",
               key_filename=r"C:\Users\Admin\.ssh\smm_bot", timeout=15)

def run(client, cmd, timeout=60):
    stdin, stdout, stderr = client.exec_command(f"bash -lc '{cmd}'", timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace") + stderr.read().decode("utf-8", errors="replace")

print(run(client, "systemctl status smm_bot | head -5"))
client.close()
```

Запуск: `py ssh_test.py` (в cmd, не PowerShell).

---

## Переменные окружения

### Обязательные

```env
BOT_TOKEN=токен_от_BotFather
ADMIN_CHAT_ID=твой_telegram_id
ANTHROPIC_API_KEY=sk-ant-...
```

### Опциональные

```env
# Модель Claude (по умолчанию claude-haiku-4-5-20251001)
CLAUDE_MODEL=claude-haiku-4-5-20251001

# Картинки
PEXELS_API_KEY=...            # https://www.pexels.com/api/
UNSPLASH_ACCESS_KEY=...       # https://unsplash.com/developers

# WB Seller API (для своих товаров)
WB_API_KEY=...
WB_API_MODE=auto              # "seller" | "search" | "auto"

# Прокси для WB парсера (если нужны)
WB_PROXY_URL=http://user:pass@host:port
WB_PROXY_URLS=http://u1:p@1.2.3.4:80,http://u2:p@5.6.7.8:80

# Буфер
BUFFER_MIN=8
BUFFER_EMERGENCY=4
BUFFER_CRITICAL=2

# Задержка публикации (сек)
POST_DELAY_MIN=300
POST_DELAY_MAX=900

# Генерация (UTC)
GENERATION_HOUR=3
GENERATION_MINUTE=0

# Debug
DEBUG=True
```

---

## Карточка канала

### Информационный канал

```json
{
  "channel_id": "@mychannel",
  "name": "Мой канал",
  "topic": "личные финансы, инвестиции",
  "audience": "широкая аудитория",
  "tone": "дружелюбный эксперт, без снобизма",
  "post_length": "100-200 слов",
  "use_emoji": true,
  "forbidden_topics": ["политика"],
  "post_formats": ["совет дня", "факт/статистика", "вопрос аудитории"],
  "rss_sources": ["https://banki.ru/xml/news.rss"],
  "evergreen_topics": ["Как составить личный бюджет"],
  "daily_posts_count": 10,
  "post_times_utc": [6, 9, 13, 17],
  "active": true
}
```

### Маркетплейс-канал (WB)

```json
{
  "channel_id": "@wbchannel",
  "name": "WB Кроссовки",
  "topic": "кроссовки, спортивная обувь",
  "source_type": "wb_parser",
  "wb_categories": ["кроссовки"],
  "post_times_utc": [6, 9, 13, 17],
  "active": true
}
```

---

## Буфер постов

| Уровень | Постов | Действие |
|---------|--------|---------|
| OK | ≥ 8 | Всё хорошо |
| Low | 4-7 | Скоро нужна генерация |
| Emergency | 2-3 | Автогенерация в фоне |
| Critical | 0-1 | Алерт + экстренная генерация |

---

## Публикация постов

`poster.py` — APScheduler каждый час. Расписание по умолчанию: 09:00, 12:00, 16:00, 20:00 МСК.

**4 попытки при публикации:**
1. Markdown + картинка
2. Plain text + картинка
3. Markdown без картинки
4. Plain text без картинки

---

## Известные технические детали

### WB API

- `/cards/v2/detail` устарел с ~2025 — используем `/cards/v4/detail`
- JSON v4: `{"products": [...]}` вместо `{"data": {"products": [...]}}`
- Basket таблица — кусочно-линейная формула (в коде `_get_basket`), проверена 2026-05
- Проверять актуальность basket таблицы раз в несколько месяцев

### SSH из Windows

- `ssh.exe` (OpenSSH Windows) через PowerShell/CMD — бывают проблемы с ключами (exit 255 без вывода)
- Решение: использовать Python `paramiko` — работает надёжно
- Запускать скрипты: `py script.py` в cmd (не PowerShell)
- SSH ключ: `C:\Users\Admin\.ssh\smm_bot`

### Git на сервере

- Если сервер имеет локальные изменения: `git checkout HEAD -- <file>` восстанавливает из коммита
- Для коммита с пробелами в сообщении использовать файл: `echo msg > msg.txt && git commit -F msg.txt`

### Anthropic API

- Блокирует запросы с российских IP (HTTP 403)
- VPS должен быть в EU/US — Amsterdam 77.233.215.77 работает

### Webshare прокси

- Аккаунт: `ryryrvfb`, пароль: `95xgdgxzev5d`
- 10 резидентных прокси в GB, CA, DE и других странах
- Для WB card API с Amsterdam VPS прокси не нужны — доступен напрямую
- Формат: `http://ryryrvfb-{country}-{N}:{password}@{ip}:80`
