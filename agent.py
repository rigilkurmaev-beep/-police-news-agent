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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Конфигурация ────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]   # твой личный chat_id
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")  # опционально

SEARCH_QUERIES = [
    "полиция МВД России новости сегодня",
    "сотрудники полиции происшествия",
    "правоохранительные органы задержание",
    "МВД полицейский подвиг награда",
    "реформа полиции закон",
    "ГИБДД ДПС новости",
    "Росгвардия новости",
    "следственный комитет новости",
]

MODEL = "claude-sonnet-4-6"
# ────────────────────────────────────────────────────────────────────────────


async def search_news(query: str, client: httpx.AsyncClient) -> list[dict]:
    """Поиск новостей через Tavily API (или fallback на DuckDuckGo RSS)."""
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
                "max_results": 5,
                "include_images": True,
                "days": 1,
            },
            timeout=15,
        )
        data = resp.json()
        results = []
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "image": (data.get("images") or [None])[0],
                "published": r.get("published_date", ""),
                "source": r.get("url", "").split("/")[2] if r.get("url") else "",
            })
        return results
    except Exception as e:
        log.warning(f"Tavily error: {e}")
        return []


async def _ddg_search(query: str, client: httpx.AsyncClient) -> list[dict]:
    """Fallback: DuckDuckGo HTML поиск (без API-ключа)."""
    try:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "df": "d"},  # df=d → за сутки
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
                if tag == "a" and "result__snippet" in attrs.get("class", ""):
                    self._in_title = False

            def handle_data(self, data):
                if self._in_title and data.strip():
                    self._current["title"] = data.strip()
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
                "image": None,
                "published": "",
                "source": r["url"].split("/")[2] if r["url"] else "",
            }
            for r in parser.results[:5]
        ]
    except Exception as e:
        log.warning(f"DDG search error: {e}")
        return []


async def analyze_and_format(raw_articles: list[dict], client: httpx.AsyncClient) -> dict:
    """
    Передаём сырые статьи в Claude.
    Получаем: дайджест + готовый пост для ВКонтакте.
    """
    today = datetime.now().strftime("%d.%m.%Y %H:%M")

    articles_text = json.dumps(raw_articles, ensure_ascii=False, indent=2)

    system_prompt = """Ты — редактор профессионального сообщества полицейских (110 000 подписчиков ВКонтакте).
Твоя задача — анализировать новости и готовить контент.
Отвечай ТОЛЬКО валидным JSON без markdown-обёртки."""

    user_prompt = f"""Сегодня: {today}

Вот собранные новости (могут быть дубли и нерелевантные):
{articles_text}

Выполни три задачи:

1. ФИЛЬТРАЦИЯ: оставь только новости, реально связанные с полицией, МВД, ГИБДД, Росгвардией, СК, правоохранительными органами России. Убери дубли.

2. ДАЙДЖЕСТ для редактора (краткий, деловой):
   - Список топ-5 новостей с оценкой важности (🔴 высокая / 🟡 средняя / 🟢 низкая)
   - Для каждой: заголовок, 1-2 предложения сути, источник, ссылка

3. ГОТОВЫЙ ПОСТ для ВКонтакте (для аудитории — действующих и бывших сотрудников полиции):
   - Заголовок с эмодзи
   - Основной текст (живой, профессиональный тон, без казённости)
   - Блок ссылок на источники
   - Хэштеги (#полиция #МВД #правоохранители и т.п.)
   - Если есть фото — укажи ссылку на изображение в поле image_url

Верни JSON строго в таком формате:
{{
  "digest": {{
    "date": "{today}",
    "total_found": <число>,
    "relevant_count": <число>,
    "items": [
      {{
        "priority": "🔴|🟡|🟢",
        "title": "...",
        "summary": "...",
        "source": "...",
        "url": "...",
        "image_url": null
      }}
    ]
  }},
  "vk_post": {{
    "text": "полный текст поста",
    "image_url": "url фото или null",
    "hashtags": "#полиция #МВД ..."
  }}
}}"""

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 3000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=60,
    )

    raw = resp.json()["content"][0]["text"]
    # Убираем возможные ```json обёртки
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


def format_digest_message(digest: dict) -> str:
    """Форматируем дайджест для Telegram."""
    lines = [
        f"📋 *ДАЙДЖЕСТ НОВОСТЕЙ* — {digest['date']}",
        f"Найдено: {digest['total_found']} | Релевантных: {digest['relevant_count']}",
        "─" * 30,
    ]
    for i, item in enumerate(digest["items"], 1):
        lines.append(
            f"{item['priority']} *{i}. {item['title']}*\n"
            f"{item['summary']}\n"
            f"📰 {item['source']}\n"
            f"🔗 {item['url']}"
        )
        if item.get("image_url"):
            lines.append(f"🖼 {item['image_url']}")
        lines.append("")
    return "\n".join(lines)


def format_vk_message(vk_post: dict) -> str:
    """Форматируем готовый VK-пост для Telegram."""
    lines = [
        "📢 *ГОТОВЫЙ ПОСТ ДЛЯ ВКОНТАКТЕ*",
        "─" * 30,
        vk_post["text"],
        "",
        vk_post["hashtags"],
    ]
    if vk_post.get("image_url"):
        lines.append(f"\n🖼 Фото: {vk_post['image_url']}")
    return "\n".join(lines)


async def send_telegram(text: str, bot_token: str, chat_id: str, client: httpx.AsyncClient):
    """Отправка сообщения в Telegram (с разбивкой если > 4096 символов)."""
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        await client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        await asyncio.sleep(0.5)


async def run_once():
    """Один цикл: поиск → анализ → отправка."""
    log.info("▶ Запуск цикла мониторинга")
    async with httpx.AsyncClient() as client:
        # 1. Сбор новостей по всем запросам
        all_articles = []
        for query in SEARCH_QUERIES:
            articles = await search_news(query, client)
            all_articles.extend(articles)
            log.info(f"  '{query}' → {len(articles)} результатов")
            await asyncio.sleep(1)  # небольшая пауза между запросами

        if not all_articles:
            log.warning("Новостей не найдено, пропускаем цикл")
            return

        log.info(f"Всего собрано: {len(all_articles)} статей")

        # 2. Анализ через Claude
        result = await analyze_and_format(all_articles, client)
        log.info(f"Claude обработал: {result['digest']['relevant_count']} релевантных новостей")

        # 3. Отправка в Telegram
        digest_msg = format_digest_message(result["digest"])
        vk_msg = format_vk_message(result["vk_post"])

        await send_telegram(digest_msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, client)
        await asyncio.sleep(1)
        await send_telegram(vk_msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, client)

        log.info("✅ Дайджест и пост отправлены в Telegram")


async def main():
    """Основной цикл: запуск каждый час."""
    log.info("🚀 Агент мониторинга новостей запущен")
    while True:
        try:
            await run_once()
        except Exception as e:
            log.error(f"Ошибка в цикле: {e}", exc_info=True)
            # Уведомление об ошибке в Telegram
            try:
                async with httpx.AsyncClient() as client:
                    await send_telegram(
                        f"⚠️ Ошибка агента:\n`{e}`",
                        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, client
                    )
            except Exception:
                pass

        log.info("⏳ Следующий запуск через 1 час")
        await asyncio.sleep(3600)  # 1 час


if __name__ == "__main__":
    asyncio.run(main())
