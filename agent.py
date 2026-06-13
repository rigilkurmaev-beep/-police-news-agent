"""
Агент мониторинга новостей для полицейского сообщества "Записки полицейского"
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
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

MODEL = "claude-sonnet-4-6"


def get_search_queries():
    """Запросы с текущим месяцем и годом для свежих новостей."""
    now = datetime.now()
    date_suffix = f"{now.strftime('%B')} {now.year}"  # например "июнь 2026"
    month_year = f"{now.month}/{now.year}"

    return [
        f"напали избили полицейского сотрудника МВД {now.year}",
        f"напали на сотрудника Росгвардии {now.year}",
        f"протаранил полицейский автомобиль ДПС {now.year}",
        f"ворвался отделение полиции напал {now.year}",
        f"задержан арестован начальник полиции МВД взятка {now.year}",
        f"полицейский осуждён уголовное дело крышевание {now.year}",
        f"сотрудник МВД Росгвардии задержан преступление {now.year}",
        f"полицейский сбил пешехода наехал скрылся {now.year}",
        f"инспектор ДПС выстрел погоня скандал {now.year}",
        f"жёсткое задержание полиция видео скандал {now.year}",
        f"некомплект кадры МВД полиция нехватка {now.year}",
        f"уволен назначен генерал МВД начальник полиции {now.year}",
        f"зарплата выплаты сотрудники МВД полиция {now.year}",
        f"реформа МВД полиция приказ закон {now.year}",
        f"полицейский спас герой подвиг награда {now.year}",
        f"сотрудник полиции Росгвардии награждён {now.year}",
    ]


FILTER_PROMPT = """Ты — редактор профессионального сообщества "Записки полицейского" (110 000 подписчиков ВКонтакте).

ГЛАВНЫЙ ПРИНЦИП: берём только СВЕЖИЕ новости (не старше 48 часов) где сотрудник или ведомство — ГЛАВНЫЙ ГЕРОЙ.

✅ БРАТЬ — МВД, полиция, Росгвардия (высший приоритет):
- Нападения на сотрудников (избили, нож, протаранили авто ДПС, ворвались в отдел)
- Коррупция и преступления сотрудников (взятка, крышевание, задержан начальник)
- Резонансные инциденты где сотрудник виновник (сбил и уехал, выстрел при погоне)
- Жёсткие/спорные задержания
- Кадровые проблемы (некомплект, увольнения, назначения)
- Реформы и законы касающиеся сотрудников (зарплаты, льготы)
- Героизм, награды, добрые дела сотрудников

✅ БРАТЬ — СК, ФСБ, прокуратура (второй приоритет):
- Только если сам сотрудник — герой события

❌ НЕ БРАТЬ:
- Новости старше 48 часов — СТРОГО
- ДТП где полиция просто приехала
- Розыск преступников (стандартная работа)
- Статистика и плановые отчёты
- Новости где полиция упомянута вскользь

ПРИОРИТЕТЫ:
🔴 high — резонанс (нападение, коррупция начальника, громкий скандал, подвиг)
🟡 medium — важное (кадры, реформы, спорное задержание)
🟢 low — интересное локальное событие"""


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
                "days": 2,  # только за последние 2 дня
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
                "published": r.get("published_date", ""),
            })
        return results
    except Exception as e:
        log.warning(f"Tavily error: {e}")
        return []


async def _ddg_search(query: str, client: httpx.AsyncClient) -> list[dict]:
    """DuckDuckGo с фильтром за последние сутки (df=d)."""
    try:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "df": "d"},  # d = последние сутки
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
                "published": "",
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
    now = datetime.now()
    today = now.strftime("%d.%m.%Y %H:%M")
    cutoff = (now - timedelta(hours=48)).strftime("%d.%m.%Y")

    articles_text = "\n".join([
        f"{i+1}. {a['title']} | {a.get('published', 'дата неизвестна')} | {a['source']} | {a['url']}"
        for i, a in enumerate(articles)
    ])

    prompt = f"""{FILTER_PROMPT}

Сейчас: {today}
Отсекай всё старше {cutoff}.

Новости для оценки:
{articles_text}

Верни JSON массив (только JSON, без markdown):
[{{"title":"...","url":"...","source":"...","priority":"high|medium|low","summary":"1-2 предложения — суть события на русском"}}]

Если ни одна не подходит или все старые — верни []"""

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
        queries = get_search_queries()
        all_articles = []
        for query in queries:
            articles = await search_news(query, client)
            all_articles.extend(articles)
            log.info(f"  '{query[:50]}' → {len(articles)} результатов")
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
        log.info(f"Релевантных свежих новостей: {len(items)}")

        if not items:
            log.info("Нет свежих релевантных новостей в этом цикле")
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
