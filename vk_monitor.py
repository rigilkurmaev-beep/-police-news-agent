"""
Агент мониторинга сообществ ВКонтакте для "Записки полицейского"
Читает посты из указанных сообществ и присылает дайджест в Telegram
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("vk_monitor.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
VK_SERVICE_TOKEN = os.environ["VK_SERVICE_TOKEN"]

MODEL = "claude-sonnet-4-6"
MEMORY_FILE = Path("/data/vk_sent_posts.json")
MEMORY_TTL_HOURS = 72

# Список сообществ для мониторинга
VK_COMMUNITIES = [
    "ombudsment",
    "typic_police",
    "russianpolice",
]

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.131"


def load_memory() -> dict:
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            cutoff = (datetime.now() - timedelta(hours=MEMORY_TTL_HOURS)).isoformat()
            data = {k: v for k, v in data.items() if v.get("sent_at", "") > cutoff}
            return data
    except Exception as e:
        log.warning(f"Ошибка загрузки памяти: {e}")
    return {}


def save_memory(memory: dict):
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Ошибка сохранения памяти: {e}")


async def get_community_posts(community: str, client: httpx.AsyncClient) -> list[dict]:
    """Получаем последние посты из сообщества через VK API."""
    try:
        resp = await client.get(
            f"{VK_API}/wall.get",
            params={
                "domain": community,
                "count": 20,
                "filter": "owner",
                "access_token": VK_SERVICE_TOKEN,
                "v": VK_VERSION,
            },
            timeout=15,
        )
        data = resp.json()

        if "error" in data:
            log.warning(f"VK API ошибка для {community}: {data['error']}")
            return []

        posts = []
        cutoff_ts = int((datetime.now() - timedelta(hours=24)).timestamp())

        for post in data.get("response", {}).get("items", []):
            # Только свежие посты за последние 24 часа
            if post.get("date", 0) < cutoff_ts:
                continue
            # Пропускаем репосты
            if "copy_history" in post:
                continue

            text = post.get("text", "").strip()
            if not text or len(text) < 30:
                continue

            post_id = f"{community}_{post['id']}"
            post_url = f"https://vk.com/{community}?w=wall-{abs(post['owner_id'])}_{post['id']}"

            posts.append({
                "id": post_id,
                "url": post_url,
                "text": text[:500],
                "date": datetime.fromtimestamp(post["date"]).strftime("%d.%m.%Y %H:%M"),
                "community": community,
            })

        log.info(f"  {community}: {len(posts)} свежих постов")
        return posts

    except Exception as e:
        log.warning(f"Ошибка получения постов {community}: {e}")
        return []


FILTER_PROMPT = """Ты — редактор профессионального сообщества "Записки полицейского" (110 000 подписчиков ВКонтакте). Аудитория — действующие и бывшие сотрудники МВД, полиции, Росгвардии.

ГЛАВНЫЙ ПРИНЦИП: берём только посты где сотрудник или ведомство — ГЛАВНЫЙ ГЕРОЙ события.

✅ БРАТЬ:
- Нападения на сотрудников (избили, нож, таран машины)
- Коррупция и преступления сотрудников (взятка, крышевание, задержан начальник)
- Героизм любого масштаба (помог человеку, спас, сопроводил в больницу)
- Погони и резонансные задержания
- Вирусные истории про сотрудников
- Суды и приговоры сотрудникам
- ДТП где сотрудник виновник
- Кадровые события (некомплект, назначения, реформы, зарплаты)
- Награды сотрудников
- Зарубежный опыт полиции

❌ НЕ БРАТЬ:
- Стандартная работа полиции (приехали на вызов, раскрыли преступление)
- Плановая статистика и отчёты
- Посты где полиция упомянута вскользь
- Общие новости без конкретного события

ПРИОРИТЕТЫ:
🔴 high — резонанс (нападение, коррупция начальника, подвиг, вирусное)
🟡 medium — важное (погоня, суд, кадры, реформы)
🟢 low — интересное (небольшая помощь, зарубежный опыт)"""


async def analyze_posts(posts: list[dict], client: httpx.AsyncClient) -> list[dict]:
    """Фильтруем посты через Claude."""
    if not posts:
        return []

    posts_text = "\n\n".join([
        f"[{i+1}] {p['community']} | {p['date']}\n{p['text'][:300]}"
        for i, p in enumerate(posts)
    ])

    prompt = f"""{FILTER_PROMPT}

Посты из ВКонтакте сообществ:
{posts_text}

Верни JSON массив (только JSON, без markdown):
[{{"index": 1, "priority": "high|medium|low", "summary": "1-2 предложения — суть события на русском"}}]

Если ничего не подходит — верни []"""

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

    results = json.loads(raw[start:end])
    items = []
    for r in results:
        idx = r.get("index", 0) - 1
        if 0 <= idx < len(posts):
            post = posts[idx].copy()
            post["priority"] = r.get("priority", "low")
            post["summary"] = r.get("summary", "")
            items.append(post)

    return items


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
    log.info("▶ Запуск мониторинга ВКонтакте")

    memory = load_memory()
    log.info(f"В памяти {len(memory)} постов за 72ч")

    async with httpx.AsyncClient() as client:
        # 1. Собираем посты из всех сообществ
        all_posts = []
        for community in VK_COMMUNITIES:
            posts = await get_community_posts(community, client)
            all_posts.extend(posts)
            await asyncio.sleep(0.5)

        log.info(f"Всего свежих постов: {len(all_posts)}")

        if not all_posts:
            log.info("Нет свежих постов")
            return

        # 2. Убираем уже отправленные
        new_posts = [p for p in all_posts if p["id"] not in memory]
        log.info(f"Новых постов (не в памяти): {len(new_posts)}")

        if not new_posts:
            log.info("Все посты уже отправлялись")
            return

        # 3. Фильтруем через Claude
        items = await analyze_posts(new_posts, client)
        log.info(f"Релевантных: {len(items)}")

        if not items:
            log.info("Нет релевантных постов")
            return

        # 4. Сортируем по приоритету
        priority_order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))

        # 5. Формируем дайджест
        today = datetime.now().strftime("%d.%m.%Y %H:%M")
        digest_lines = [f"📱 *ДАЙДЖЕСТ ВКОНТАКТЕ* — {today}\n"]
        for i, item in enumerate(items, 1):
            emoji = priority_emoji(item.get("priority", "low"))
            digest_lines.append(
                f"{emoji} *{i}. [{item['community']}]* {item['date']}\n"
                f"{item.get('summary', '')}\n"
                f"🔗 {item.get('url', '')}\n"
            )
        digest = "\n".join(digest_lines)

        await send_telegram(digest, client)

        # 6. Сохраняем в память
        now_iso = datetime.now().isoformat()
        for item in items:
            memory[item["id"]] = {
                "summary": item.get("summary", ""),
                "sent_at": now_iso,
            }
        save_memory(memory)

        log.info(f"✅ Отправлено {len(items)} постов из ВКонтакте")


async def main():
    log.info("🚀 Агент мониторинга ВКонтакте запущен")
    while True:
        try:
            await run_once()
        except Exception as e:
            log.error(f"Ошибка: {e}", exc_info=True)
            try:
                async with httpx.AsyncClient() as client:
                    await send_telegram(f"⚠️ Ошибка VK агента:\n`{e}`", client)
            except Exception:
                pass

        log.info("⏳ Следующий запуск через 1 час")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
