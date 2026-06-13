"""
Агент мониторинга новостей для полицейского сообщества "Записки полицейского"
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
    # Нападения и атаки на сотрудников
    "напали избили полицейского сотрудника МВД",
    "напали на сотрудника Росгвардии",
    "протаранил полицейский автомобиль ДПС инспектор",
    "ворвался отделение полиции напал сотрудников",
    # Коррупция и преступления сотрудников
    "задержан арестован начальник полиции МВД взятка коррупция",
    "полицейский осуждён уголовное дело крышевание",
    "сотрудник МВД задержан преступление",
    "сотрудник Росгвардии задержан коррупция",
    # Резонансные инциденты с участием сотрудников
    "полицейский сбил пешехода наехал скрылся",
    "инспектор ДПС выстрел погоня погиб",
    "полицейский превысил применил силу скандал",
    "жёсткое задержание полиция видео скандал",
    # Кадры и реформы МВД
    "некомплект кадры МВД полиция нехватка сотрудников",
    "уволен назначен генерал МВД начальник полиции",
    "зарплата выплаты льготы сотрудники МВД полиция 2026",
    "реформа МВД полиция приказ закон 2026",
    # Героизм и позитив
    "полицейский спас герой подвиг награда медаль",
    "сотрудник полиции Росгвардии награждён звание",
]

MODEL = "claude-sonnet-4-6"

FILTER_PROMPT = """Ты — редактор профессионального сообщества "Записки полицейского" (110 000 подписчиков ВКонтакте). Аудитория — действующие и бывшие сотрудники МВД, полиции, Росгвардии.

ГЛАВНЫЙ ПРИНЦИП: берём только новости где сотрудник или ведомство — ГЛАВНЫЙ ГЕРОЙ события, а не просто упомянуты.

✅ БРАТЬ — МВД и полиция (высший приоритет):
- Нападения и атаки на сотрудников (избили, напали с ножом, протаранили авто ДПС, ворвались в отдел)
- Коррупция и преступления самих сотрудников (взятка, крышевание, задержан начальник ОВД)
- Резонансные инциденты где сотрудник виновник (сбил пешехода и уехал, выстрел при погоне, превышение силы)
- Жёсткие/спорные задержания с применением силы
- Кадровые проблемы (некомплект, приглашения вернуться на службу, увольнения)
- Назначения и отставки руководителей МВД
- Реформы, законы, приказы напрямую касающиеся сотрудников (зарплаты, льготы, форма)
- Героизм: спас человека, подвиг при исполнении, награды
- Позитивные истории про сотрудников (почётный донор, добрые дела)
- Скандалы внутри ведомства

✅ БРАТЬ — Росгвардия (равный приоритет с МВД):
- Те же критерии: нападения на сотрудников, коррупция, резонансные инциденты, кадры, реформы, героизм

✅ БРАТЬ — СК, ФСБ, прокуратура (второй приоритет):
- Только если сам сотрудник/руководитель — герой события (задержан, награждён, скандал)

❌ НЕ БРАТЬ:
- ДТП, преступления, происшествия где полиция просто выехала и работает как обычно
- Розыск преступников (стандартная работа)
- Обеспечение порядка на мероприятиях
- Плановая статистика и отчёты
- Новости где полиция/МВД упомянуты вскользь как фон

ПРИОРИТЕТЫ:
🔴 high — резонансное событие (нападение на сотрудника, коррупция начальника, громкий скандал, подвиг)
🟡 medium — важное но менее резонансное (кадровые изменения, реформы, спорное задержание)
🟢 low — интересное но небольшое событие (местная новость, позитив районного масштаба)"""


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
    if not articles:
        return []

    articles = articles[:20]
    today = datetime.now().strftime("%d.%m.%Y %H:%M")

    articles_text = "\n".join([
        f"{i+1}. {a['title']} | {a['source']} | {a['url']}"
        for i, a in enumerate(articles)
    ])

    prompt = f"""{FILTER_PROMPT}

Дата: {today}
Новости для оценки:
{articles_text}

Верни JSON массив (только JSON, без markdown):
[{{"title":"...","url":"...","source":"...","priority":"high|medium|low","summary":"1-2 предложения — суть события на русском, кто главный герой и что произошло"}}]

Если ни одна не подходит — верни []"""

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )

    raw = resp.json()["content"][0]["text"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end == 0:
        return []

    return json.loads(raw[start:end])


async def send_telegram(text: str, client: httpx.AsyncClient):
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
        all_articles = []
        for query in SEARCH_QUERIES:
            articles = await search_news(query, client)
            all_articles.extend(articles)
            log.info(f"  '{query[:45]}' → {len(articles)} результатов")
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

        items = await analyze_news(unique, client)
        log.info(f"Релевантных новостей: {len(items)}")

        if not items:
            log.info("Нет релевантных новостей в этом цикле")
            return

        # Сортируем по приоритету
        priority_order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))

        today = datetime.now().strftime("%d.%m.%Y %H:%M")
        digest_lines = [f"📋 *ЗАПИСКИ ПОЛИЦЕЙСКОГО* — {today}\n"]
        for i, item in enumerate(items, 1):
            emoji = priority_emoji(item.get("priority", "low"))
            digest_lines.append(
                f"{emoji} *{i}. {item['title']}*\n"
                f"{item.get('summary', '')}\n"
                f"📰 {item.get('source', '')} | {item.get('url', '')}\n"
            )
        digest = "\n".join(digest_lines)

        await send_telegram(digest, client)
        log.info("✅ Дайджест отправлен в Telegram")


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
