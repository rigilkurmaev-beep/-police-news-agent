"""
Агент мониторинга новостей для полицейского сообщества
Запускается каждый час, ищет новости, форматирует и отправляет в Telegram
"""

import os
import json
import asyncio
import logging
from datetime import datetime
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

SEARCH_QUERIES = [
    "полиция МВД России новости сегодня",
    "сотрудники полиции происшествия",
    "правоохранительные органы задержание",
    "МВД полицейский подвиг награда",
    "ГИБДД ДПС новости",
    "Росгвардия новости",
]

MODEL = "claude-sonnet-4-6"


async def search_news(query: str, client: httpx.AsyncClient) -> list[dict]:
    if TAVILY_API_KEY:
        return await _tavily_search(query, client)
    return await _ddg_search(query, client)


async def _tavily_search(query: str, client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 3,
                "include_images": False,
                "days": 1,
            },
            timeout=15,
        )
        data = resp.json()
        results = []
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", "")[:200],
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:300],
                "source": r.get("url", "").split("/")[2] if r.get("url") else "",
            })
        return results
    except Exception as e:
        log.warning(f"Tavily error: {e}")
        return []


async def _ddg_search(query: str, client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "df": "d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
            follow_redirects=True,
        )
        from html.parser import HTMLParser

        class DDGParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.results = []
                self._in_title = False
                self._current = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "a" and "result__a" in attrs.get("class", ""):
                    self._current["url"] = attrs.get("href", "")
                    self._in_title = True

            def handle_data(self, data):
                if self._in_title and data.strip():
                    self._current["title"] = data.strip()[:200]
                    self._in_title = False
                    if self._current.get("url"):
                        self.results.append(dict(self._current))
                        self._current = {}

        parser = DDGParser()
        parser.feed(resp.text)
        return [
            {
                "title": r["title"],
                "url": r["url"],
                "snippet": "",
                "source": r["url"].split("/")[2] if r["url"] else "",
            }
            for r in parser.results[:3]
        ]
    except Exception as e:
        log.warning(f"DDG search error: {e}")
        return []


async def analyze_news(articles: list[dict], client: httpx.AsyncClient) -> list[dict]:
    """Фильтрация и оценка релевантности через Claude."""
    if not articles:
        return []

    # Берём максимум 15 статей чтобы не перегружать контекст
    articles = articles[:15]
    today = datetime.now().strftime("%d.%m.%Y %H:%M")

    articles_text = "\n".join([
        f"{i+1}. {a['title']} | {a['source']} | {a['url']}"
        for i, a in enumerate(articles)
    ])

    prompt = f"""Дата: {today}
Список новостей (заголовок | источник | ссылка):
{articles_text}

Отбери только новости про полицию, МВД, ГИБДД, Росгвардию, СК, правоохранительные органы России.
Верни JSON массив (без markdown, только JSON):
[{{"title":"...","url":"...","source":"...","priority":"high|medium|low","summary":"1-2 предложения на русском"}}]
Если релевантных нет — верни пустой массив []"""

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )

    raw = resp.json()["content"][0]["text"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    # Находим JSON массив в ответе
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end == 0:
        return []

    return json.loads(raw[start:end])


async def format_vk_post(items: list[dict], client: httpx.AsyncClient) -> str:
    """Генерируем готовый пост для ВКонтакте."""
    if not items:
        return ""

    top = items[:3]
    news_text = "\n".join([
        f"- {it['title']} ({it['source']})"
        for it in top
    ])

    prompt = f"""Напиши пост для ВКонтакте для профессионального сообщества полицейских (110 тыс подписчиков).

Новости для поста:
{news_text}

Требования:
- Живой профессиональный тон, без казённости
- Эмодзи в заголовке
- 3-5 предложений основного текста
- Ссылки на источники в конце
- Хэштеги: #полиция #МВД #правоохранители

Верни только текст поста, без пояснений."""

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )

    return resp.json()["content"][0]["text"].strip()


async def send_telegram(text: str, client: httpx.AsyncClient):
    """Отправка в Telegram с разбивкой на части."""
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            log.error(f"Telegram send error: {e}")
        await asyncio.sleep(0.5)


def priority_emoji(p: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(p, "⚪")


async def run_once():
    log.info("▶ Запуск цикла мониторинга")
    async with httpx.AsyncClient() as client:
        # 1. Сбор новостей
        all_articles = []
        for query in SEARCH_QUERIES:
            articles = await search_news(query, client)
            all_articles.extend(articles)
            log.info(f"  '{query}' → {len(articles)} результатов")
            await asyncio.sleep(1)

        # Убираем дубли по URL
        seen = set()
        unique = []
        for a in all_articles:
            if a["url"] not in seen:
                seen.add(a["url"])
                unique.append(a)

        log.info(f"Уникальных статей: {len(unique)}")

        if not unique:
            log.warning("Новостей не найдено")
            return

        # 2. Анализ через Claude
        items = await analyze_news(unique, client)
        log.info(f"Релевантных новостей: {len(items)}")

        if not items:
            log.info("Нет релевантных новостей, пропускаем")
            return

        # 3. Формируем дайджест
        today = datetime.now().strftime("%d.%m.%Y %H:%M")
        digest_lines = [f"📋 *ДАЙДЖЕСТ* — {today}\n"]
        for i, item in enumerate(items, 1):
            emoji = priority_emoji(item.get("priority", "low"))
            digest_lines.append(
                f"{emoji} *{i}. {item['title']}*\n"
                f"{item.get('summary', '')}\n"
                f"📰 {item.get('source', '')} | {item.get('url', '')}\n"
            )
        digest = "\n".join(digest_lines)

        # 4. Генерируем VK пост
        vk_post = await format_vk_post(items, client)

        # 5. Отправляем в Telegram
        await send_telegram(digest, client)
        await asyncio.sleep(1)
        if vk_post:
            await send_telegram(f"📢 *ГОТОВЫЙ ПОСТ ДЛЯ ВКОНТАКТЕ*\n{'─'*20}\n{vk_post}", client)

        log.info("✅ Отправлено в Telegram")


async def main():
    log.info("🚀 Агент мониторинга новостей запущен")
    while True:
        try:
            await run_once()
        except Exception as e:
            log.error(f"Ошибка: {e}", exc_info=True)
            try:
                async with httpx.AsyncClient() as client:
                    await send_telegram(f"⚠️ Ошибка агента:\n`{e}`", client)
            except Exception:
                pass

        log.info("⏳ Следующий запуск через 1 час")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
