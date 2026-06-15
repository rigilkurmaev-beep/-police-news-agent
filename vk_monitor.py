"""
Агент мониторинга сообществ ВКонтакте для "Записки полицейского"
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
            if post.get("date", 0) < cutoff_ts:
                continue
            if "copy_history" in post:
                continue

            text = post.get("text", "").strip()
            if not text:
                continue

            owner_id = int(post['owner_id'])
            pid = int(post['id'])
            post_url = f"https://vk.com/{community}?w=wall{owner_id}_{pid}"
            post_id = f"{community}_{pid}"

            posts.append({
                "id": post_id,
                "url": post_url,
                "text": text[:1000],
                "date": datetime.fromtimestamp(post["date"]).strftime("%d.%m.%Y %H:%M"),
                "community": community,
            })

        log.info(f"  {community}: {len(posts)} свежих постов")
        return posts

    except Exception as e:
        log.warning(f"Ошибка получения постов {community}: {e}")
        return []


FILTER_PROMPT = """Ты — редактор профессионального сообщества "Записки полицейского" (110 000 подписчиков).

Тебе присылают посты из тематических полицейских сообществ ВКонтакте. Твоя задача — отсеять только явно нерелевантное и пропустить всё интересное.

✅ ПРОПУСКАТЬ (почти всё что связано с полицией и правоохранителями):
- Любые новости и события с участием полицейских, сотрудников МВД, ГАИ, ДПС, Росгвардии, СК, ФСБ
- Коррупция, преступления, суды над сотрудниками
- Героизм, подвиги, награды
- Погони, задержания, операции
- Зарубежный опыт полиции (США, Европа, другие страны) — интересно читателям
- Исторические материалы про известных сотрудников (например дело Евсюкова)
- Курьёзные и вирусные истории с участием полиции
- Кадровые новости, реформы, законы
- Аналитика и расследования про МВД и правоохранителей

❌ ОТКЛОНЯТЬ только явно нерелевантное:
- Реклама и коммерческие предложения
- Поздравления с праздниками без новостного содержания
- Посты совсем не про полицию и правоохранителей
- Технические объявления сообщества

ВАЖНО: Эти сообщества специализируются на полицейской тематике — доверяй их отбору. Если пост хоть как-то связан с полицией/правоохранителями — пропускай.

ПРИОРИТЕТЫ:
🔴 high — резонанс (коррупция начальника, нападение, громкий приговор, вирусное)
🟡 medium — важное событие (погоня, задержание, кадры, реформы)
🟢 low — интересное (зарубежный опыт, история, курьёз)"""


async def analyze_posts(posts: list[dict], client: httpx.AsyncClient) -> list[dict]:
    if not posts:
        return []

    posts_text = "\n\n".join([
        f"[{i+1}] {p['community']} | {p['date']}\n{p['text']}"
        for i, p in enumerate(posts)
    ])

    prompt = f"""{FILTER_PROMPT}

Посты для оценки:
{posts_text}

Верни JSON массив (только JSON, без markdown):
[{{"index": 1, "priority": "high|medium|low", "summary": "1-2 предложения — суть поста на русском"}}]

Если пост нерелевантен — не включай его. Если все релевантны — включай все."""

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
        all_posts = []
        for community in VK_COMMUNITIES:
            posts = await get_community_posts(community, client)
            all_posts.extend(posts)
            await asyncio.sleep(0.5)

        log.info(f"Всего свежих постов: {len(all_posts)}")

        if not all_posts:
            log.info("Нет свежих постов")
            return

        new_posts = [p for p in all_posts if p["id"] not in memory]
        log.info(f"Новых постов (не в памяти): {len(new_posts)}")

        if not new_posts:
            log.info("Все посты уже отправлялись")
            return

        items = await analyze_posts(new_posts, client)
        log.info(f"Релевантных: {len(items)}")

        if not items:
            log.info("Нет релевантных постов")
            return

        priority_order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))

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
