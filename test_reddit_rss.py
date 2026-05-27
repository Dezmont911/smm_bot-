import sys, io, feedparser
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

urls = [
    "https://www.reddit.com/r/Minecraft/.rss",
    "https://www.reddit.com/r/personalfinance/.rss",
    "https://www.reddit.com/r/cars/.rss",
    "https://medium.com/feed/tag/cryptocurrency",
]

for url in urls:
    feed = feedparser.parse(url)
    entries = feed.entries[:3]
    print(f"URL: {url}")
    print(f"  Статей: {len(feed.entries)}")
    for e in entries:
        print(f"  - {e.get('title', '?')[:70]}")
    print()
