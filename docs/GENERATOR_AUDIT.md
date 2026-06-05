# Generator Audit

Документ фиксирует текущее состояние генератора контента и план улучшений без изменения кода. Цель аудита: понять, почему посты могут становиться однотипными, уходить в общие AI/news/gaming темы и плохо попадать в конкретный канал, например локальную школу робототехники для детей.

## 1. Текущая схема генератора

Основной pipeline:

```text
Channel Card
  -> Source Collection
  -> Topic Selection
  -> Topic Dedup / Relevance Gate
  -> Content Router
  -> Prompt Builder
  -> LLM Generation
  -> Image Selection / Generation
  -> Semantic Dedup
  -> Buffer
  -> Publishing
```

В реальных файлах:

```text
channels/*.json
  -> content_generator.py:_collect_topics
  -> topic_search.py / rss_parser.py / web_scraper.py / evergreen_topics / fallback
  -> content_generator.py:_topic_already_used / _filter_relevant
  -> content_router.py:resolve / pick_format / pick_hook
  -> ai_client.py:_build_system_prompt / _build_user_prompt / generate_post
  -> image_fetcher.py / image_generator.py / RSS image_url
  -> content_generator.py:_is_duplicate
  -> buffer_manager.py:add
  -> poster.py:tick / _process_channel / _publish
```

### Source Collection

`content_generator.py:_collect_topics` собирает кандидаты в таком порядке:

1. `topic_search.py:get_topics`, если `channel["topic_source"] == "search"`.
2. `rss_parser.py:fetch_for_channel`.
3. `web_scraper.py:scrape_for_channel`, если RSS дал мало тем.
4. `buffer_manager.py:get_evergreen_topic`.
5. Fallback-углы из `channel["topic"]` или `channel["name"]`.

Если web-search дал достаточно тем, RSS и web-scraper не вызываются. Это значит, что `topic_source="search"` может фактически стать главным источником тем.

### Topic Selection

Выбор темы сейчас в основном основан на порядке источников и количестве кандидатов. Сложного ранжирования под конкретный канал, цель бизнеса, аудиторию и CTA нет.

RSS скорит статьи по свежести, совпадению ключевых слов из `channel["topic"]` и наличию картинки. Web-search просит Claude найти свежие инфоповоды по теме канала. Web-scraper подбирает Reddit/Medium RSS-источники через Claude и сохраняет их в карточку канала как `web_sources`.

### Dedup And Relevance

Текущий dedup:

- `content_generator.py:_get_used_topics` берет последние темы канала из `posts` со статусами `published`, `ready`, `skipped`.
- `content_generator.py:_topic_already_used` ловит точное совпадение или почти полное пересечение слов.
- `content_generator.py:_is_duplicate` сравнивает готовый текст с недавними постами этого же канала.
- `dedup.py` использует локальную sentence-transformers модель, если она доступна; иначе падает в lexical fallback.
- `config.py:DEDUP_THRESHOLD` задает порог semantic dedup для готовых постов.

Текущий relevance gate:

- `content_generator.py:_filter_relevant` работает только если backend embeddings доступен.
- Профиль канала строится из `topic`, `name`, `image_keywords`.
- `audience`, `goal`, `CTA`, pain points и запрещенные углы в профиль не входят.
- `evergreen` и `fallback` темы не проверяются.
- Если после фильтра осталось мало кандидатов, часть отсеянных тем возвращается обратно.

### Prompt And LLM

`ai_client.py:_build_system_prompt` передает Claude:

- название канала;
- тему канала;
- аудиторию;
- длину поста;
- use_emoji;
- глобальные и per-channel forbidden topics;
- до двух example posts;
- последние использованные темы;
- style guidance из `content_router.py`;
- общий `_HUMAN_VOICE`.

`tone` считывается, но как отдельная строка профиля в system prompt фактически не используется. Продуктовая цель канала, оффер, локальность, CTA и боли аудитории отдельными полями не передаются.

`ai_client.py:_build_user_prompt` передает формат, структурный hook и тему/инфоповод. Content Brief перед генерацией отсутствует.

### Buffer And Publishing

`buffer_manager.py:add` кладет готовый пост в таблицу `posts`. По умолчанию статус `ready`.

`poster.py:tick` каждый час проходит по активным каналам. `poster.py:_process_channel` проверяет `schedule_disabled`, часы публикации, pending RSY overlay, минимальный gap между публикациями, затем берет `buffer.get_next(channel_id)` и публикует через `_publish`.

## 2. Список ключевых файлов

| File | Role |
| --- | --- |
| `content_generator.py` | Главный orchestrator генерации, сбор тем, dedup, image flow, запись в buffer |
| `ai_client.py` | Prompt builder и генерация текста через Claude |
| `content_router.py` | Выбор стратегии канала: archetype, style, format weights, hooks |
| `archetypes.py` | Пресеты архетипов и их style/format настройки |
| `topic_search.py` | Web-search тем через Claude, cache тем |
| `rss_parser.py` | RSS parsing, scoring, image extraction |
| `web_scraper.py` | Подбор Reddit/Medium RSS и чтение этих источников |
| `dedup.py` | Embedding backend и cosine similarity |
| `buffer_manager.py` | Очередь постов, статусы, evergreen topics, reference/media attach |
| `poster.py` | Публикация постов по расписанию и immediate publish |
| `database.py` | SQLite schema: channels, posts, topic_cache, evergreen_topics, costs |
| `channel_analyzer.py` | Анализ канала, выбор archetype/topic_source, fallback analysis |
| `bot.py` | Команды, onboarding, добавление каналов, schedule, handlers |
| `ui.py` | Inline UI, настройки каналов, admin panel, queue/review actions |
| `reference_importer.py` | Импорт постов из reference channels |
| `image_fetcher.py` | Поиск внешних картинок |
| `image_generator.py` | Генерация изображений через image provider |

## 3. Найденные проблемы

### P0: Нет Channel DNA

Карточка канала хранит тему, аудиторию, tone, forbidden topics, post formats и archetype, но не хранит полноценную ДНК канала:

- цель канала;
- бизнес-оффер;
- локальность;
- портрет читателя;
- боли читателя;
- что обязательно упоминать;
- какие углы запрещены;
- какой CTA нужен;
- какие типы тем подходят, а какие нет.

Из-за этого генератор знает "про что канал", но не знает "зачем канал пишет".

### P0: Нет Content Brief перед генерацией

Сейчас LLM получает формат и тему. Между выбранной темой и генерацией нет промежуточного брифа:

```text
Тема -> Пост
```

Нужен слой:

```text
Тема -> Content Brief -> Пост
```

Brief должен фиксировать аудиторию, цель поста, угол, CTA, запреты и критерии качества.

### P0: Relevance Gate слабый

Текущий `_filter_relevant` полезен, но недостаточен:

- зависит от embeddings backend;
- использует только `topic`, `name`, `image_keywords`;
- не учитывает audience/goal/CTA;
- не проверяет evergreen/fallback;
- может вернуть часть отсеянных тем обратно, чтобы не оставить буфер пустым.

Это хорошо для устойчивости буфера, но плохо для строгого попадания в конкретный бизнес-канал.

### P1: Источники тем могут уводить канал в новости

`topic_source="search"` просит свежие инфоповоды. Для новостных каналов это нормально. Для локального бизнеса это часто ошибка: свежие инфоповоды по робототехнике, AI, coding или games не равны полезному посту для родителей.

`web_scraper.py` подбирает Reddit/Medium источники. Для детской школы робототехники он может подобрать широкие programming/gaming/AI источники, если тема описана слишком общо.

### P1: Стилизация слишком широкая

`content_router.py` и `archetypes.py` уже решают проблему "один промпт на всех", но архетипы слишком широкие. Есть gaming/news/finance/etc, но нет отдельного профиля для:

- детского образования;
- локального сервиса;
- кружков и секций;
- parent marketing;
- edtech для родителей.

Поэтому канал школы робототехники может получить `default` или неподходящую соседнюю рамку.

### P1: Недостаточная память генератора

Сейчас память есть на уровне последних тем и semantic dedup готовых текстов. Но нет долговременной памяти:

- последние углы;
- последние CTA;
- последние форматы между запусками;
- какие боли уже закрывали;
- какие темы были слабые;
- какие темы были отклонены;
- какие посты реально опубликованы и с каким результатом.

### P2: Нет global dedup

Dedup работает внутри одного `channel_id`. Если много похожих каналов, одинаковые темы и углы могут повторяться между каналами.

### P2: Quality Gate после генерации слабый

После LLM есть проверки на пустой ответ, meta/refusal и forbidden content. Но нет проверки:

- соответствует ли пост Channel DNA;
- есть ли нужный CTA;
- написан ли пост для правильной аудитории;
- не ушел ли пост в общий AI/news tone;
- не является ли тема нерелевантной, несмотря на красивый текст.

## 4. План внедрения

### P0. Channel DNA

Добавить в карточку канала структурный блок, например:

```json
{
  "channel_dna": {
    "audience": "родители детей 4-15 лет",
    "goal": "приводить заявки в DM/WhatsApp на пробное занятие",
    "offer": "локальная школа робототехники и программирования для детей",
    "locality": "город/район, если задан",
    "pain_points": [
      "ребенок много играет и мало создает",
      "родитель не понимает, рано ли начинать программирование",
      "хочется полезное занятие вместо бесцельного экрана"
    ],
    "allowed_topic_types": [
      "польза занятий",
      "ответы на вопросы родителей",
      "примеры навыков детей",
      "разбор мифов",
      "мягкий продающий пост"
    ],
    "forbidden_angles": [
      "новости игровой индустрии",
      "релизы Nintendo/Steam/консолей",
      "взрослая IT-карьера без связи с детьми",
      "абстрактные AI новости"
    ],
    "cta": "написать в WhatsApp/DM и записаться на пробное занятие"
  }
}
```

Минимально безопасный подход: поддержать этот блок как optional. Если его нет, старые каналы продолжают работать по текущей логике.

### P0. Content Router

Расширить routing не только до style/format, но и до стратегии темы:

```text
channel_dna + archetype + topic_source -> generation_strategy
```

Для локальных бизнесов стратегия должна отличаться от news/gaming:

- меньше web-search;
- больше evergreen/business topics;
- обязательная проверка на audience fit;
- CTA-friendly форматы;
- запрет на "просто новости индустрии".

Новые archetypes:

- `kids_education`;
- `local_service`;
- `parent_marketing`;
- `edtech`;
- возможно `hobby_school`.

### P0. Content Brief

Перед `ai_client.generate_post` строить brief:

```text
topic
  -> content_brief = {
       angle,
       target_reader,
       post_goal,
       must_include,
       must_avoid,
       CTA,
       format_constraints
     }
  -> prompt
```

Brief можно строить rule-based для начала, без отдельного LLM-вызова, чтобы не увеличивать стоимость.

Пример для школы робототехники:

```text
Тема: "Ребенок любит игры"
Угол: "как превратить интерес к играм в создание своих проектов"
Аудитория: родители детей 7-12 лет
Цель: показать пользу занятий и привести на пробное
CTA: "напишите в WhatsApp, подберем группу по возрасту"
Нельзя: новости игровых релизов, взрослый IT-тон, обещания гарантированной профессии
```

### P1. Semantic Dedup

Усилить dedup на уровне тем и углов:

- хранить `topic_embedding`;
- хранить `angle`;
- сравнивать не только готовый `content`, но и `topic + angle`;
- учитывать `pending_review`, `ready`, `published`, `skipped`;
- добавить отдельный порог для topic/angle similarity;
- логировать причину отклонения.

Для 40+ каналов можно добавить optional global dedup:

```text
same owner + similar archetype + recent period -> avoid same topic/angle
```

### P1. Memory Layer

Добавить память канала:

- последние темы;
- последние углы;
- последние CTA;
- последние форматы;
- rejected topics;
- successful published topics;
- возможно engagement stats, когда появится аналитика.

Минимально можно начать с таблицы или JSON-поля:

```json
{
  "generation_memory": {
    "last_angles": [],
    "last_formats": [],
    "last_ctas": [],
    "rejected_topics": []
  }
}
```

Использование:

- `Content Router` не выбирает тот же формат/угол подряд между разными запусками;
- `Content Brief` получает последние углы и избегает повторов;
- `Relevance Gate` сохраняет rejected topics с причиной.

## 5. Отдельный раздел: локальная школа робототехники для детей

### Что канал должен делать

Для такого канала посты должны быть не просто "про робототехнику", а про ценность занятий для родителей и детей.

Целевая аудитория:

- родители детей 4-15 лет;
- родители, которые ищут полезную секцию;
- родители, которые переживают из-за игр/экранов;
- родители, которым важно развитие логики, самостоятельности и уверенности ребенка.

Цели постов:

- объяснять пользу робототехники и программирования;
- снимать страхи родителей;
- показывать понятные примеры занятий;
- мягко вести к пробному занятию;
- давать CTA в DM/WhatsApp.

### Почему сейчас могут появляться плохие темы

Если тема канала описана как "робототехника, программирование, дети", система может считать близкими:

- AI новости;
- игровые релизы;
- Nintendo/Steam/console news;
- взрослую IT-карьеру;
- общие посты про coding.

Для embedding/relevance это может быть "рядом", но для бизнеса это off-topic.

### Что должно считаться хорошими темами

Хорошие темы:

- "Почему робототехника помогает ребенку учиться думать по шагам";
- "Что делать, если ребенок только играет, но не хочет ничего создавать";
- "С какого возраста можно начинать программирование";
- "Как пробное занятие помогает понять, подходит ли ребенку кружок";
- "Почему ошибки на занятиях полезны";
- "Что ребенок забирает с курса кроме робота";
- "Как выбрать группу по возрасту";
- "Почему LEGO/роботы - это не просто игрушки".

Плохие темы:

- "Nintendo Direct показала новые игры";
- "Новая модель AI научилась писать код";
- "Лучшие языки программирования для зарплаты";
- "Новости игровой индустрии";
- "Топ игр недели";
- "Как взрослому войти в IT".

### Рекомендуемая DNA для такого канала

```json
{
  "channel_dna": {
    "audience": "родители детей 4-15 лет",
    "goal": "запись на пробное занятие",
    "offer": "занятия робототехникой и программированием для детей",
    "tone": "понятный, теплый, уверенный, без взрослого IT-жаргона",
    "pain_points": [
      "ребенок слишком много играет",
      "непонятно, рано ли начинать",
      "хочется полезное занятие после школы",
      "родитель боится, что ребенку будет сложно",
      "нужно развивать логику и самостоятельность"
    ],
    "allowed_topic_types": [
      "польза навыков",
      "ответы на вопросы родителей",
      "мифы о программировании для детей",
      "что происходит на занятии",
      "истории учеников",
      "мягкий CTA на пробное"
    ],
    "forbidden_angles": [
      "игровые новости",
      "консольные релизы",
      "взрослая IT-карьера",
      "абстрактные AI новости",
      "слишком технические посты без связи с детьми"
    ],
    "cta": "напишите в DM/WhatsApp, подберем группу по возрасту и пригласим на пробное занятие"
  }
}
```

### Минимальный safe plan для этого канала

1. Завести optional `channel_dna` в карточке канала.
2. Добавить strict relevance check для `kids_education/local_service`.
3. В prompt передавать Content Brief с аудиторией, целью и CTA.
4. Запретить gaming/news topics через `forbidden_angles`.
5. Начать хранить последние углы, чтобы посты не повторяли одну и ту же мысль.

## 6. Отдельные технические наблюдения

`ui.py` содержит callback route `ui:generate_all`, который вызывает `gen.run_for_all_channels()`. В `content_generator.py` найден `run_morning_batch()` и `run_top_up_cycle()`, но метод `run_for_all_channels()` не найден. Это не часть качества генерации, но перед ручным массовым запуском эту кнопку стоит проверить отдельно.

## 7. Рекомендуемый порядок следующих аудитов

1. Аудит `channel_analyzer.py`: как определяется topic/archetype/topic_source для разных ниш.
2. Аудит `content_generator.py:_collect_topics`: как сделать source strategy по типу канала.
3. Аудит prompts в `ai_client.py`: добавить Content Brief без ломки текущего поведения.
4. Аудит `dedup.py` и `posts.embedding`: semantic topic/angle dedup.
5. Аудит UI настроек: как удобно редактировать Channel DNA без перегруза интерфейса.
