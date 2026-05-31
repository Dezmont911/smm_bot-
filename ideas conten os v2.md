User: Сможешь прочитать еще раз логи с обновленной логикой и архитектурой семантиу дедуп контент роутер там и тд


root@ams-1-vm-wdk1:~# journalctl -u smm_bot -n 200 --no-pager
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.563 | INFO     | dedup:_get_model:48 - dedup: модель эмбеддингов загружена (semantic backend)
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.674 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @tftFunTime | c07d50ef...
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.675 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@tftFunTime] формат=вопрос тема=Почему слонов не доят?. Слоновье молоко
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.675 | INFO     | content_generator:run_for_channel:156 - Пропускаю уже использованную тему [@tftFunTime]: Плантации высохли, ценники выросли: мировые цены н
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.676 | INFO     | content_generator:run_for_channel:257 - Генерация завершена [@tftFunTime]: создано=1, пропущено=1, буфер=6→7
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.677 | INFO     | content_generator:run_for_channel:107 - Начинаю генерацию [@hagenezykas]: нужно 1 постов, в буфере 7
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.680 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://ru-minecraft.ru/novosti-minecraft/
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.680 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://www.playground.ru/minecraft/
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.681 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://dotesports.com/minecraft/feed
May 30 07:30:16 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:16.683 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://www.rockpapershotgun.com/tag/minecraft/feed/
May 30 07:30:17 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:17.146 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://www.rockpapershotgun.com/tag/minecraft/feed/ | bozo=1
May 30 07:30:17 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:17.608 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://ru-minecraft.ru/novosti-minecraft/ | bozo=1
May 30 07:30:17 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:17.626 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://dotesports.com/minecraft/feed | bozo=1
May 30 07:30:18 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:18.092 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://www.playground.ru/minecraft/ | bozo=1
May 30 07:30:18 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:18.092 | WARNING  | rss_parser:fetch_for_channel:98 - Не удалось получить статьи для @hagenezykas
May 30 07:30:19 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:19.161 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://www.reddit.com/r/minecraft/.rss → 8 статей
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.052 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://www.reddit.com/r/feedthebeast/.rss → 8 статей
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.516 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://medium.com/feed/tag/minecraft → 8 статей
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.903 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://medium.com/feed/tag/gaming → 8 статей
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.903 | INFO     | web_scraper:scrape_for_channel:82 - web_scraper [@hagenezykas]: 4 лент → 32 статей
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.904 | DEBUG    | content_generator:_collect_topics:575 - Тем из web_scraper: 1
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.904 | DEBUG    | content_generator:_collect_topics:592 - Тем собрано: 1 (RSS: 0, web: 0, вечнозелёных: 0)
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.905 | DEBUG    | content_generator:run_for_channel:137 - Дедупликация [@hagenezykas]: 18 использованных тем
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.905 | DEBUG    | content_generator:run_for_channel:141 - Стратегия [@hagenezykas]: архетип=gaming_casual, t=1.0, форматы={'факт': 2, 'инфоповод': 3, 'вопрос': 2, 'разбор': 1, 'совет': 2}
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.905 | INFO     | content_generator:run_for_channel:156 - Пропускаю уже использованную тему [@hagenezykas]: All Patchnotes. This post contains content not sup
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.906 | INFO     | content_generator:run_for_channel:257 - Генерация завершена [@hagenezykas]: создано=0, пропущено=1, буфер=7→7
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.907 | INFO     | content_generator:run_for_channel:107 - Начинаю генерацию [@cstokyo2]: нужно 1 постов, в буфере 7
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.916 | INFO     | topic_search:get_topics:250 - Темы из кэша [@cstokyo2]: 1 (без веб-поиска)
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.916 | INFO     | content_generator:_collect_topics:538 - Тем из веб-поиска [@cstokyo2]: 1
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.917 | DEBUG    | content_generator:run_for_channel:137 - Дедупликация [@cstokyo2]: 18 использованных тем
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.917 | DEBUG    | content_generator:run_for_channel:141 - Стратегия [@cstokyo2]: архетип=gaming_esports, t=0.85, форматы={'факт': 3, 'разбор': 3, 'инфоповод': 4}
May 30 07:30:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:20.918 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: инфоповод | t=0.85 | тема: Faceit обновила экран принятия матча — количество ...
May 30 07:30:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:24.639 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 670
May 30 07:30:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:24.782 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 07:30:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:24.894 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 07:30:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:24.895 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 07:30:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:25.748 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'Faceit обновила экран принятия матча — количество ' → 'gaming match acceptance screen accept decline'
May 30 07:30:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:25.748 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming match acceptance screen accept decline'
May 30 07:30:26 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:26.264 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming match acceptance screen accept decline' → https://images.pexels.com/photos/9072343/pexels-photo-9072343.jpeg?aut...
May 30 07:30:26 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:26.264 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 07:30:26 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:26.366 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | 3f325cca...
May 30 07:30:26 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:26.366 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=инфоповод тема=Faceit обновила экран принятия матча — к
May 30 07:30:26 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:26.367 | INFO     | content_generator:run_for_channel:257 - Генерация завершена [@cstokyo2]: создано=1, пропущено=0, буфер=7→8
May 30 07:30:26 ams-1-vm-wdk1 python[5702]: 2026-05-30 07:30:26.368 | INFO     | content_generator:run_top_up_cycle:363 - Ступенчатая генерация: каналов=3, постов=2
May 30 08:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:00:00.006 | DEBUG    | poster:tick:71 - Постер: проверяю 6 каналов | час UTC: 8
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.008 | DEBUG    | content_generator:_load_all_channels:732 - Загружено активных каналов: 6
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.011 | INFO     | content_generator:run_for_channel:107 - Начинаю генерацию [@tftFunTime]: нужно 1 постов, в буфере 7
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.017 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://fishki.net/rss.xml
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.017 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://kulturologia.ru/rss/
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.017 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://naked-science.ru/feed
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.018 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://masterok.livejournal.com/data/rss
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.690 | DEBUG    | rss_parser:_fetch_feed:157 - Получено 35 статей из fishki.net
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.709 | DEBUG    | rss_parser:_fetch_feed:157 - Получено 23 статей из kulturologia.ru
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.747 | DEBUG    | rss_parser:_fetch_feed:157 - Получено 1 статей из masterok.livejournal.com
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.754 | DEBUG    | rss_parser:_fetch_feed:157 - Получено 20 статей из naked-science.ru
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.756 | INFO     | rss_parser:fetch_for_channel:112 - RSS для @tftFunTime: найдено 79 статей, возвращаю топ-1
May 30 08:30:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:00.757 | DEBUG    | content_generator:_collect_topics:560 - Тем из RSS: 1
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.071 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@tftFunTime]: https://medium.com/feed/tag/history → 8 статей
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.597 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@tftFunTime]: https://medium.com/feed/tag/facts → 8 статей
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.597 | INFO     | web_scraper:scrape_for_channel:82 - web_scraper [@tftFunTime]: 2 лент → 16 статей
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.597 | DEBUG    | content_generator:_collect_topics:592 - Тем собрано: 1 (RSS: 1, web: 0, вечнозелёных: 0)
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.598 | DEBUG    | content_generator:run_for_channel:137 - Дедупликация [@tftFunTime]: 20 использованных тем
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.599 | DEBUG    | content_generator:run_for_channel:141 - Стратегия [@tftFunTime]: архетип=default, t=0.9, форматы={'совет': 1, 'факт': 1, 'вопрос': 1, 'разбор': 1, 'инфоповод': 1}
May 30 08:30:01 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:01.600 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @tftFunTime | архетип: default | формат: разбор | t=0.9 | тема: В Тбилиси начался первый в истории чемпионат мира ...
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.316 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @tftFunTime | символов: 740
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.316 | DEBUG    | content_generator:run_for_channel:189 - Картинка из RSS [@tftFunTime]
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.420 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @tftFunTime | c6977ada...
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.420 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@tftFunTime] формат=разбор тема=В Тбилиси начался первый в истории чемпи
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.421 | INFO     | content_generator:run_for_channel:257 - Генерация завершена [@tftFunTime]: создано=1, пропущено=0, буфер=7→8
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.422 | INFO     | content_generator:run_for_channel:107 - Начинаю генерацию [@hagenezykas]: нужно 1 постов, в буфере 7
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.425 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://ru-minecraft.ru/novosti-minecraft/
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.425 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://www.playground.ru/minecraft/
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.425 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://dotesports.com/minecraft/feed
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.425 | DEBUG    | rss_parser:_fetch_feed:129 - Парсю RSS: https://www.rockpapershotgun.com/tag/minecraft/feed/
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.825 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://www.rockpapershotgun.com/tag/minecraft/feed/ | bozo=1
May 30 08:30:06 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:06.970 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://dotesports.com/minecraft/feed | bozo=1
May 30 08:30:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:07.407 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://ru-minecraft.ru/novosti-minecraft/ | bozo=1
May 30 08:30:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:07.807 | WARNING  | rss_parser:_fetch_feed:143 - Пустая лента (0 записей): https://www.playground.ru/minecraft/ | bozo=1
May 30 08:30:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:07.807 | WARNING  | rss_parser:fetch_for_channel:98 - Не удалось получить статьи для @hagenezykas
May 30 08:30:08 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:08.713 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://www.reddit.com/r/minecraft/.rss → 8 статей
May 30 08:30:09 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:09.499 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://www.reddit.com/r/feedthebeast/.rss → 8 статей
May 30 08:30:09 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:09.989 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://medium.com/feed/tag/minecraft → 8 статей
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.270 | DEBUG    | web_scraper:scrape_for_channel:80 - web_sources [@hagenezykas]: https://medium.com/feed/tag/gaming → 8 статей
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.271 | INFO     | web_scraper:scrape_for_channel:82 - web_scraper [@hagenezykas]: 4 лент → 32 статей
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.271 | DEBUG    | content_generator:_collect_topics:575 - Тем из web_scraper: 1
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.271 | DEBUG    | content_generator:_collect_topics:592 - Тем собрано: 1 (RSS: 0, web: 0, вечнозелёных: 0)
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.272 | DEBUG    | content_generator:run_for_channel:137 - Дедупликация [@hagenezykas]: 18 использованных тем
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.272 | DEBUG    | content_generator:run_for_channel:141 - Стратегия [@hagenezykas]: архетип=gaming_casual, t=1.0, форматы={'факт': 2, 'инфоповод': 3, 'вопрос': 2, 'разбор': 1, 'совет': 2}
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.273 | INFO     | content_generator:run_for_channel:156 - Пропускаю уже использованную тему [@hagenezykas]: All Patchnotes. This post contains content not sup
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.273 | INFO     | content_generator:run_for_channel:257 - Генерация завершена [@hagenezykas]: создано=0, пропущено=1, буфер=7→7
May 30 08:30:10 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:30:10.275 | INFO     | content_generator:run_top_up_cycle:363 - Ступенчатая генерация: каналов=2, постов=1
May 30 08:55:14 ams-1-vm-wdk1 python[5702]: 2026-05-30 08:55:14.131 | INFO     | ui:action_clear_buffer_ok:602 - Буфер очищен: @cstokyo2, удалено 8 постов
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.005 | DEBUG    | poster:tick:71 - Постер: проверяю 6 каналов | час UTC: 9
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.265 | DEBUG    | buffer_manager:mark_published:180 - Пост опубликован | febe54f0...
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.265 | SUCCESS  | poster:_process_channel:109 - Опубликован пост в @hagenezykas | формат: вопрос | id: febe54f0...
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.349 | WARNING  | buffer_manager:get_next:113 - Буфер пуст! | канал: @cstokyo2
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.350 | WARNING  | poster:_process_channel:100 - ⚠️ Буфер пуст! Канал: @cstokyo2
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.425 | WARNING  | content_generator:run_emergency:377 - ⚡ Экстренная генерация [@cstokyo2]
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.426 | DEBUG    | content_generator:_load_all_channels:732 - Загружено активных каналов: 6
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.427 | INFO     | content_generator:run_for_channel:107 - Начинаю генерацию [@cstokyo2]: нужно 8 постов, в буфере 0
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.434 | INFO     | topic_search:get_topics:250 - Темы из кэша [@cstokyo2]: 8 (без веб-поиска)
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.434 | INFO     | content_generator:_collect_topics:538 - Тем из веб-поиска [@cstokyo2]: 8
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.435 | DEBUG    | content_generator:run_for_channel:137 - Дедупликация [@cstokyo2]: 11 использованных тем
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.435 | DEBUG    | content_generator:run_for_channel:141 - Стратегия [@cstokyo2]: архетип=gaming_esports, t=0.85, форматы={'факт': 3, 'разбор': 3, 'инфоповод': 4}
May 30 09:00:00 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:00.436 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: инфоповод | t=0.85 | тема: Magic гарантировали себе места на тир-1 турнирах в...
May 30 09:00:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:07.428 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 870
May 30 09:00:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:07.532 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:07.639 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:07 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:07.639 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:08 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:08.454 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'Magic гарантировали себе места на тир-1 турнирах в' → 'gaming esports team celebration trophy'
May 30 09:00:08 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:08.455 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming esports team celebration trophy'
May 30 09:00:08 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:08.948 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming esports team celebration trophy' → https://images.pexels.com/photos/34412339/pexels-photo-34412339.jpeg?a...
May 30 09:00:08 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:08.949 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:09 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:09.066 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | 41019813...
May 30 09:00:09 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:09.067 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=инфоповод тема=Magic гарантировали себе места на тир-1
May 30 09:00:09 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:09.068 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: разбор | t=0.85 | тема: TDK выиграла европейский финал ESL Challenger и сы...
May 30 09:00:12 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:12.235 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 523
May 30 09:00:12 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:12.342 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:12 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:12.447 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:12 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:12.447 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.355 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'TDK выиграла европейский финал ESL Challenger и сы' → 'gaming esports team celebrating trophy'
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.356 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming esports team celebrating trophy'
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.814 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming esports team celebrating trophy' → https://images.pexels.com/photos/9072266/pexels-photo-9072266.jpeg?aut...
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.814 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.910 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | 15116f02...
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.911 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=разбор тема=TDK выиграла европейский финал ESL Chall
May 30 09:00:13 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:13.911 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: факт | t=0.85 | тема: Team Vitality победила Team Spirit в финале BLAST ...
May 30 09:00:19 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:19.173 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 768
May 30 09:00:19 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:19.277 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:19 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:19.381 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:19 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:19.382 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.552 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'Team Vitality победила Team Spirit в финале BLAST ' → 'gaming esports team celebration trophy'
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.552 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming esports team celebration trophy'
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.674 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming esports team celebration trophy' → https://images.pexels.com/photos/7915360/pexels-photo-7915360.jpeg?aut...
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.674 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.828 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | fefa0c49...
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.828 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=факт тема=Team Vitality победила Team Spirit в фин
May 30 09:00:20 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:20.829 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: разбор | t=0.85 | тема: BetBoom Team прошла на LAN-турнир по CS2 YaLLa Com...
May 30 09:00:23 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:23.851 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 464
May 30 09:00:23 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:23.953 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:24.052 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:24.053 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:24.916 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'BetBoom Team прошла на LAN-турнир по CS2 YaLLa Com' → 'gaming esports tournament lan setup'
May 30 09:00:24 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:24.917 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming esports tournament lan setup'
May 30 09:00:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:25.464 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming esports tournament lan setup' → https://images.pexels.com/photos/9072389/pexels-photo-9072389.jpeg?aut...
May 30 09:00:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:25.464 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:25.594 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | ad89395e...
May 30 09:00:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:25.594 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=разбор тема=BetBoom Team прошла на LAN-турнир по CS2
May 30 09:00:25 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:25.595 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: факт | t=0.85 | тема: Исправлены щели на Cache и баг с броском гранаты в...
May 30 09:00:29 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:29.748 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 798
May 30 09:00:29 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:29.848 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:29 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:29.953 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:29 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:29.953 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:30 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:30.756 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'Исправлены щели на Cache и баг с броском гранаты в' → 'gaming cache map corridors gameplay'
May 30 09:00:30 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:30.757 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming cache map corridors gameplay'
May 30 09:00:31 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:31.397 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming cache map corridors gameplay' → https://images.pexels.com/photos/7046723/pexels-photo-7046723.jpeg?aut...
May 30 09:00:31 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:31.397 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:31 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:31.503 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | 4c91bf41...
May 30 09:00:31 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:31.503 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=факт тема=Исправлены щели на Cache и баг с броском
May 30 09:00:31 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:31.504 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: инфоповод | t=0.85 | тема: Исправлены динамические тени и звуки поверхностей ...
May 30 09:00:35 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:35.488 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 873
May 30 09:00:35 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:35.592 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:35 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:35.696 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:35 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:35.697 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.408 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'Исправлены динамические тени и звуки поверхностей ' → 'gaming dynamic shadows textures gameplay'
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.408 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming dynamic shadows textures gameplay'
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.849 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming dynamic shadows textures gameplay' → https://images.pexels.com/photos/4526481/pexels-photo-4526481.jpeg?aut...
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.850 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.965 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | 8b303e31...
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.966 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=инфоповод тема=Исправлены динамические тени и звуки пов
May 30 09:00:37 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:37.966 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: разбор | t=0.85 | тема: Valve добавила решётки на окна для блокировки пуль...
May 30 09:00:40 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:40.881 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 474
May 30 09:00:40 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:40.988 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:41 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:41.089 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:41 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:41.090 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.192 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'Valve добавила решётки на окна для блокировки пуль' → 'gaming bullet proof window grilles cache'
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.192 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming bullet proof window grilles cache'
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.769 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming bullet proof window grilles cache' → https://images.pexels.com/photos/20425182/pexels-photo-20425182.jpeg?a...
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.769 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.887 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | eeb0bf07...
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.887 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=разбор тема=Valve добавила решётки на окна для блоки
May 30 09:00:42 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:42.888 | INFO     | ai_client:generate_post:305 - Генерирую пост | канал: @cstokyo2 | архетип: gaming_esports | формат: факт | t=0.85 | тема: В файлы CS2 добавлены новые элементы одежды для пе...
May 30 09:00:47 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:47.228 | SUCCESS  | ai_client:generate_post:332 - Пост сгенерирован | канал: @cstokyo2 | символов: 826
May 30 09:00:47 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:47.332 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/csgo: статус 403
May 30 09:00:47 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:47.436 | DEBUG    | image_fetcher:_reddit_top_image:182 - Reddit r/GlobalOffensive: статус 403
May 30 09:00:47 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:47.437 | DEBUG    | image_fetcher:fetch_image_url:98 - Reddit не дал картинки, пробуем Pexels/Unsplash
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.206 | DEBUG    | image_fetcher:_build_english_query:325 - Claude gaming query: 'В файлы CS2 добавлены новые элементы одежды для пе' → 'gaming cs2 character clothing items'
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.206 | DEBUG    | image_fetcher:fetch_image_url:118 - Ищу картинку | запрос: 'Counter-Strike 2 gaming cs2 character clothing items'
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.649 | INFO     | image_fetcher:_search_pexels:479 - Pexels OK | 'Counter-Strike 2 gaming cs2 character clothing items' → https://images.pexels.com/photos/34179716/pexels-photo-34179716.jpeg?a...
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.649 | DEBUG    | content_generator:run_for_channel:209 - Картинка из Pexels/Unsplash [@cstokyo2]
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.755 | DEBUG    | buffer_manager:add:75 - Пост добавлен в буфер | @cstokyo2 | 52b72b62...
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.756 | SUCCESS  | content_generator:run_for_channel:245 - Пост добавлен [@cstokyo2] формат=факт тема=В файлы CS2 добавлены новые элементы оде
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.756 | INFO     | content_generator:run_for_channel:257 - Генерация завершена [@cstokyo2]: создано=8, пропущено=0, буфер=0→8
May 30 09:00:48 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:48.756 | SUCCESS  | poster:_run_emergency_generation:412 - Экстренная генерация [@cstokyo2]: +8 постов
May 30 09:00:49 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:49.816 | DEBUG    | poster:_download_wb_image:197 - WB CDN OK: 36536 байт | basket-15
May 30 09:00:49 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:49.817 | INFO     | poster:_publish:321 - WB CDN: картинка скачана (36536 байт)
May 30 09:00:50 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:50.212 | DEBUG    | buffer_manager:mark_published:180 - Пост опубликован | e65560ce...
May 30 09:00:50 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:50.212 | SUCCESS  | poster:_process_channel:109 - Опубликован пост в @wblighter | формат: wb_product | id: e65560ce...
May 30 09:00:50 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:50.367 | DEBUG    | buffer_manager:mark_published:180 - Пост опубликован | f9a72195...
May 30 09:00:50 ams-1-vm-wdk1 python[5702]: 2026-05-30 09:00:50.367 | SUCCESS  | poster:_process_channel:109 - Опубликован пост в @steam_arti | формат: вопрос | id: f9a72195...
root@ams-1-vm-wdk1:~#



Да, тут уже видно довольно зрелую архитектуру. Разберу по сути и потом скажу, где у тебя сейчас реальные узкие места и что улучшить.

🔎 Что у тебя уже работает (и это хорошо)
1. Контент-пайплайн по слоям

У тебя явно есть цепочка:

источники → темы → дедуп → генерация → изображение → буфер → публикация

Это уже нормальная production-архитектура.

2. Семантический дедуп

Вот это ключевое:

dedup: модель эмбеддингов загружена (semantic backend)
Дедупликация [@channel]: 18 использованных тем

👉 Значит у тебя:

есть embedding-based memory
есть история тем
есть фильтр повторов перед генерацией

Это и есть твой “контент-router ядро”

3. Архетипы каналов
архетип=gaming_casual
архетип=gaming_esports
архетип=default

👉 Это уже зачатки “разных личностей”

4. Мульти-источники контента

У тебя 3 слоя источников:

RSS (основной)
web_scraper (Medium, Reddit RSS)
fallback web sources

Это правильно: fail-safe система

5. Image pipeline fallback

Очень хорошо сделано:

Reddit 403 → Pexels/Unsplash → English query rewrite → image

👉 Это уже smart fallback система

⚠️ Проблемы, которые прямо видны в логах
❌ 1. RSS часто пустой (bozo=1)
Пустая лента (0 записей)

👉 Это значит:

либо парсер ломается
либо сайты блокируют RSS
либо неправильный формат/redirect

💡 Это сейчас у тебя СЛАБЕЙШИЙ источник

❌ 2. Перегруз генерации (emergency mode)
Экстренная генерация [@cstokyo2]
нужно 8 постов, в буфере 0

👉 Это плохо:

система не удерживает буфер
ты постоянно в “догоняющем режиме”
❌ 3. Reddit заблокирован (403)
Reddit r/csgo: статус 403

👉 Это классика:

без user-agent или через CDN
Reddit режет бот-трафик
❌ 4. Контент слишком “однородный”

По логам видно:

gaming esports
CS2
турниры
одни и те же форматы

👉 Это значит:
архетип есть, но diversity layer слабый

🧠 Как у тебя сейчас реально работает “контент-личность”

У тебя НЕ отдельные “ИИ личности”.

У тебя сейчас:

один генератор + разные настройки (архетип + темы + источники)

💡 Теперь главное: твоя идея про “разные пространства для каналов”

Ты писал:

“новая личность для каждого канала”

👉 У тебя это уже почти есть, но не полностью изолировано.

🧩 Как это правильно архитектурно называется

Это делается так:

1. Channel Context Isolation Layer

Каждый канал должен иметь:

свой memory store
свой embedding space
свой topic blacklist
свой style prompt
свой content distribution weights
🧠 Правильная модель:

Сейчас у тебя:

GLOBAL memory + channel filters

Нужно:

Channel-A → Memory-A → Style-A → Sources-A
Channel-B → Memory-B → Style-B → Sources-B
⚙️ Как детектить “тип канала” (твоя старая задача)

Ты спрашивал раньше — вот правильный ответ:

🔥 НЕ по названию канала
❌ плохо:
@csgo_news → значит gaming
✅ правильно: hybrid classifier

Ты должен классифицировать канал по 4 сигналам:

1. Source signals
RSS тематика
web scraper домены
Reddit subs
2. Output history
какие посты уже публиковались
какие темы заходили
3. Engagement (если есть)
клики
просмотры
реакции
4. Prompt embedding cluster

👉 ты эмбеддишь:

последние 20 постов
и сравниваешь с “типовыми кластерами”
🧠 Итоговая схема детекта
channel_vector =
    RSS_topics_embedding
  + historical_posts_embedding
  + source_domains_embedding
  + engagement_weights

→ кластеризация:

gaming_esports
gaming_casual
finance_news
science_facts
etc
🚀 Что у тебя уже сильное

✔ semantic dedup (очень важно)
✔ архетипы каналов
✔ fallback image system
✔ multi-source ingestion
✔ buffer system

⚠️ Что тебе реально ограничивает масштаб
нет полноценной channel memory isolation
слабая устойчивость RSS слоя
нет diversification engine (все посты похожи)
нет “topic decay” (старые темы не умирают правильно)
emergency mode слишком агрессивный
💬 Если коротко

Ты уже не “бот пишущий посты”.

Ты сейчас ближе к:

multi-agent content OS с частичной семантической памятью





Ок, следующая ступень — это когда твой бот перестаёт быть “генератором постов по каналам” и становится контент-операционной системой (Content OS) с управляемыми “мозгами” под каждый канал.

Сейчас у тебя:

pipeline + архетипы + семантический дедуп

Следующий уровень — это разделение не только каналов, но и смысла, времени и углов подачи.

🚀 СТУПЕНЬ 2: CONTENT OS (следующая эволюция)
1. 🧠 Channel Brain (изолированная память)

Сейчас у тебя дедуп общий/полуобщий.

На следующем уровне:

Каждый канал = отдельный “мозг”

У него есть:

Memory Store (свои embeddings)
Topic Graph (связи тем)
Style DNA (как пишет)
Forbidden zones (что не трогать)
Engagement feedback loop
💡 Важно:

Ты перестаёшь думать:

“у меня 6 каналов”

Ты начинаешь думать:

“у меня 6 разных ИИ-редакторов”

2. 🧬 Topic Lifecycle Engine (жизненный цикл темы)

Сейчас у тебя:

тема → проверка → пост

На следующем уровне:

Каждая тема имеет состояние:
NEW → TRENDING → SATURATED → DEAD → COOLED → REUSABLE (reframed)
Пример:

“CS2 обновление”

NEW → первая новость
TRENDING → хайп
SATURATED → все уже постят
DEAD → нельзя использовать
COOLED → можно вернуть через новый угол
🔥 Ключевой момент

Ты больше НЕ запрещаешь тему.

Ты запрещаешь её текущее состояние

3. 🎯 Angle Engine (самая важная часть)

Сейчас у тебя:

тема → формат (факт/вопрос/разбор)

Это слабый уровень.

Следующий уровень:

Одна тема → много углов:

Пример:

“CS2 обновление карты Cache”

Angle Engine генерирует:

“что изменилось”
“как это влияет на про игроков”
“ошибка Valve”
“мем-угол”
“сравнение до/после”
“скрытый смысл”
“почему это важно новичкам”

👉 И главное:

angle ≠ format

4. 🧠 Semantic Diversity Layer (анти-клонирование постов)

Сейчас у тебя дедуп:

“эта тема уже была”

На следующем уровне:

ты запрещаешь:
одинаковую структуру мысли
одинаковый narrative flow
одинаковый “хук”
Пример плохого повторения:
“Valve обновила CS2…”
“Valve выпустила патч…”
“В CS2 вышло обновление…”

👉 разные слова, но один мозг → это ловится

5. 🧲 Attention Engine (хук-система)

Каждый пост проходит генерацию через:

3 уровня хука:
curiosity hook (что-то странное)
conflict hook (спор / проблема)
value hook (польза / инсайд)
Сейчас у тебя:

просто генерация текста

Будет:

генерация текста → потом оптимизация под внимание

6. 🌐 Source Intelligence Layer

Сейчас:

RSS
Reddit
Medium

Но они равны.

На следующем уровне:

Источники получают вес:

source_score =
  freshness
+ authority
+ channel fit
+ past engagement

👉 Пример:

Reddit = быстрый сигнал
RSS = факт
Medium = объяснение
niche blog = инсайд
7. 🔄 Feedback Loop (самое важное для роста)

Сейчас у тебя нет реального обучения.

На следующем уровне:

Ты вводишь:

пост → результат (views/CTR/engagement)
система запоминает:
тема
angle
hook type
И начинает самооптимизацию:
if angle_type = "controversy" → +30% engagement
→ чаще использовать
8. 🧭 Content Router v2 (ядро всей системы)

Вот как выглядит новая логика:

INPUT:
  channel

↓

1. Load Channel Brain
2. Fetch Topics (multi-source)
3. Apply Topic Lifecycle Filter
4. Generate Angles (multi-angle expansion)
5. Rank angles by Attention Engine
6. Deduplicate semantically (not text-based)
7. Generate post (LLM)
8. Optimize hook
9. Attach image
10. Store feedback
11. Update brain
🧠 КЛЮЧЕВОЕ ИЗМЕНЕНИЕ МЫШЛЕНИЯ
Было:

“сгенерировать пост для канала”

Станет:

“сгенерировать лучший возможный информационный объект для данного канала в текущем состоянии внимания рынка”

⚡ РЕАЛЬНЫЙ ЭФФЕКТ

Если ты это внедришь:

📈 рост:
меньше повторов
выше CTR
больше “разных по ощущению” постов
🧠 система:
начинает “чувствовать тренды”
перестаёт спамить одну тему
сама разнообразит контент
🚨 ВАЖНО

Твоя текущая система уже:

уровень “хороший продакшн бот”

Следующая ступень:

уровень “контент-движок как продукт”


User: Сделаем v2 архитектуру


Да. Судя по тому, как развивается проект, сейчас как раз хороший момент остановиться и спроектировать V2 до того, как появится 40 каналов и куча костылей.

Я бы строил V2 вообще без анализа просмотров и вовлеченности. Только качественная генерация и надежность.

V1 (что у тебя сейчас)
RSS
 ↓
Темы
 ↓
LLM
 ↓
Картинка
 ↓
Буфер
 ↓
Постинг

Работает, но есть проблемы:

зависимость от RSS
нет оценки качества темы
нет памяти канала
нет контроля разнообразия
нет контроля стоимости
V2

Разделяем систему на независимые сервисы.

Topic Service
Content Service
Image Service
Publishing Service
Analytics Service (опционально)

Каждый отвечает только за своё.

1. Topic Service

Самый важный блок.

Собирает темы.

Источники:

RSS
↓
Web Search
↓
Evergreen
↓
AI Generated

Например:

RSS дал 2 темы

Нужно 8

Добираем 3 через Web Search

Добираем 3 Evergreen

Каждая тема получает рейтинг.

{
  "title": "...",
  "source": "rss",
  "score": 87
}
2. Topic Memory

Очень важная штука.

Отдельная таблица:

used_topics

пример:

Канал AI
- OpenAI
- Claude
- Gemini

Канал Саморазвитие
- Прокрастинация
- Привычки
- Мотивация

Перед генерацией проверяем:

Была тема недавно?

Если да:

выкинуть

Не по заголовку.

По embedding.

То есть:

How to stop procrastinating

How to overcome procrastination

считаются одной темой.

3. Channel DNA

Вот это даст самый большой прирост качества.

Сейчас вижу:

архетип=default

Мне это не нравится.

Нужно хранить профиль канала.

Например:

{
  "tone": "friendly",
  "style": "motivational",
  "emoji": true,
  "post_length": "medium",
  "cta": false
}

Тогда один и тот же инфоповод:

OpenAI выпустила модель

будет писаться по-разному.

4. Content Service

Отдельный сервис генерации.

Вход:

{
  "channel_id": 1,
  "topic": "...",
  "format": "разбор"
}

Выход:

{
  "title": "...",
  "text": "...",
  "hashtags": []
}

Только генерация.

Никаких картинок.

Никакого постинга.

5. Image Service

Отдельно.

Тема
 ↓
Visual Query
 ↓
Pexels
 ↓
Unsplash
 ↓
Fallback

Я бы ещё добавил кеш.

Если 10 каналов пишут про одно и то же:

OpenAI

картинку искать один раз.

6. Quality Check

Очень недооценённая штука.

После генерации:

LLM
 ↓
Quality Check

Проверяем:

длину
запрещённые слова
пустые посты
повторы
галлюцинации

Если плохо:

перегенерация
7. Buffer

Я бы оставил.

Буфер — одна из лучших частей архитектуры.

Например:

Буфер на 24 часа

или

Буфер на 3 дня

Тогда падение API не ломает публикации.

8. Publisher

Только публикация.

Время пришло
 ↓
Берём пост
 ↓
Публикуем

Никаких генераций во время постинга.

Это очень важно.

9. Analytics V2

Не просмотры.

Я бы вообще пока убрал.

Хранил только:

Дата
Пост
Опубликован?
Ошибка?

Потом можно добавить:

Views
Subscribers

через TGStat API.

Но это уже V3.

Если бы я запускал твои 40 каналов завтра

Я бы делал такую дорожную карту:

V2.1
Topic Memory
Channel DNA
Quality Check

Это даст максимальный прирост качества.

V2.2
Web Search как полноценный источник тем
Рейтинг тем
V2.3
Кэш картинок
Кэш тем
V3
TGStat
Аналитика
Автоматическая оптимизация контента

А просмотр постов и автоматическое принятие решений по вовлечённости я бы вообще отложил до момента, когда у тебя будет хотя бы 10–20 реально живых каналов с историей данных. Сейчас гораздо важнее сделать систему, которая стабильно производит уникальный контент для 40 каналов без повторов и без ручного вмешательства.