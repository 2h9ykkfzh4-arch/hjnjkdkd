
import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ========== НАСТРОЙКИ ==========

BOT_TOKEN = "9005048762:AAHNZ3WyJKMk9d7SaN1XsKi8R52cMUSAklA"
GEMINI_API_KEY = "AQ.Ab8RN6IS4Mi_JUvjokkYVrUcDON3KSnEIUs
Hvt3hN1X5bbX0rQ"
ADMIN_ID = 8256340679

TIMEZONE = ZoneInfo("Europe/Moscow")
DAILY_HOUR = 6
DAILY_MINUTE = 30

MAX_VIDEOS = 100
SCROLLS_PER_PAGE = 12
SCROLL_DELAY = 2.0

HASHTAGS = [
    "мото", "мотоцикл", "мотозапчасти", "мотообзор",
    "мотоэкипировка", "мототюнинг", "ремонтмото",
    "motorcycle", "motorcycleparts", "bikelife", "motovlog"
]

PROFILE_DIR = "tiktok_profile"
DB_FILE = "scout.db"

# ========== СЛУЖЕБНЫЕ ОБЪЕКТЫ ==========

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("danil-irbis")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

scan_lock = asyncio.Lock()
stop_scan = asyncio.Event()

login_context = None
login_playwright = None
scan_task = None

ai = genai.Client(api_key=GEMINI_API_KEY) if (
    GEMINI_API_KEY and not GEMINI_API_KEY.startswith("ВСТАВЬ_")
) else None


# ========== БАЗА ДАННЫХ ==========

def db_init():
    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                url TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                found_at TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                found INTEGER,
                status TEXT
            )
        """)


def save_video(item):
    with sqlite3.connect(DB_FILE) as con:
        cur = con.execute("""
            INSERT OR IGNORE INTO videos
            (url, title, source, views, likes, comments, shares, found_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["url"], item["title"], item["source"],
            item["views"], item["likes"], item["comments"],
            item["shares"], datetime.now(TIMEZONE).isoformat()
        ))
        return cur.rowcount == 1


def top_videos(limit=10):
    with sqlite3.connect(DB_FILE) as con:
        return con.execute("""
            SELECT title, url, views, likes, comments, shares
            FROM videos
            ORDER BY (views + likes * 5 + comments * 8 + shares * 10) DESC
            LIMIT ?
        """, (limit,)).fetchall()


def count_videos():
    with sqlite3.connect(DB_FILE) as con:
        return con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def log_run(found, status):
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            "INSERT INTO runs(started_at, found, status) VALUES (?, ?, ?)",
            (datetime.now(TIMEZONE).isoformat(), found, status)
        )


# ========== ПРОВЕРКИ И ДОСТУП ==========

def is_admin(message: Message):
    return message.from_user and message.from_user.id == ADMIN_ID


async def admin_only(message: Message):
    if not is_admin(message):
        await message.answer("Доступ запрещён.")
        return False
    return True


def parse_count(value):
    if not value:
        return 0
    value = str(value).lower().replace(",", ".").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)([kкmм]?)", value)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2)
    if suffix in ("k", "к"):
        number *= 1_000
    elif suffix in ("m", "м"):
        number *= 1_000_000
    return int(number)


def parse_metrics(text):
    text = text or ""
    numbers = re.findall(
        r"\d+(?:[.,]\d+)?\s*[kкmм]?",
        text.lower()
    )
    values = [parse_count(x) for x in numbers]
    values += [0, 0, 0, 0]
    return values[0], values[1], values[2], values[3]


# ========== БРАУЗЕР ==========

async def create_browser():
    global login_playwright

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=True,
        viewport={"width": 1280, "height": 800},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote"
        ]
    )
    return pw, context


async def page_is_captcha(page):
    try:
        text = (await page.locator("body").inner_text(timeout=5000)).lower()
        markers = [
            "verify to continue",
            "captcha",
            "подтвердите, что вы не робот",
            "проверка безопасности",
            "перетащите ползунок",
            "security verification"
        ]
        return any(marker in text for marker in markers)
    except Exception:
        return False


async def collect_page(page, source, results):
    for _ in range(SCROLLS_PER_PAGE):
        if stop_scan.is_set() or len(results) >= MAX_VIDEOS:
            break

        if await page_is_captcha(page):
            raise RuntimeError("CAPTCHA")

        links = await page.locator('a[href*="/video/"]').evaluate_all("""
            nodes => nodes.map(a => ({
                url: a.href,
                text: (a.innerText || '') + ' ' +
                      (a.getAttribute('aria-label') || '') + ' ' +
                      (a.getAttribute('title') || '')
            }))
        """)

        for link in links:
            url = link.get("url", "").split("?")[0]
            if "/video/" not in url or url in results:
                continue

            title = re.sub(r"\\s+", " ", link.get("text", "")).strip()
            views, likes, comments, shares = parse_metrics(title)

            results[url] = {
                "url": url,
                "title": title[:500] or "Название не найдено",
                "source": source,
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares
            }

            if len(results) >= MAX_VIDEOS:
                break

        await page.evaluate("window.scrollBy(0, 1800)")
        await asyncio.sleep(SCROLL_DELAY)

    return results


async def scan_tiktok():
    results = {}

    pw, context = await create_browser()
    try:
        page = context.pages[0] if context.pages else await context.new_page()

        # Не загружаем тяжёлые видео и шрифты — экономим память сервера.
        async def route_handler(route):
            if route.request.resource_type in ("media", "font"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)

        for hashtag in HASHTAGS:
            if stop_scan.is_set() or len(results) >= MAX_VIDEOS:
                break

            url = f"https://www.tiktok.com/tag/{hashtag}"
            log.info("Открываю %s", url)

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45000
                )
                await asyncio.sleep(3)

                if await page_is_captcha(page):
                    raise RuntimeError("CAPTCHA")

                await collect_page(page, f"#{hashtag}", results)

            except RuntimeError:
                raise
            except PlaywrightTimeoutError:
                log.warning("Тайм-аут страницы %s", hashtag)
            except Exception as exc:
                log.exception("Ошибка на хештеге %s: %s", hashtag, exc)

        # Лента рекомендаций — отдельный источник.
        if not stop_scan.is_set() and len(results) < MAX_VIDEOS:
            try:
                await page.goto(
                    "https://www.tiktok.com/foryou",
                    wait_until="domcontentloaded",
                    timeout=45000
                )
                await asyncio.sleep(4)
                if await page_is_captcha(page):
                    raise RuntimeError("CAPTCHA")
                await collect_page(page, "For You", results)
            except RuntimeError:
                raise
            except Exception as exc:
                log.warning("Не удалось прочитать For You: %s", exc)

    finally:
        await context.close()
        await pw.stop()

    return list(results.values())[:MAX_VIDEOS]


# ========== ИИ-АНАЛИЗ ==========

async def analyze_with_ai(videos):
    if not ai or not videos:
        return "ИИ-анализ отключён: проверь GEMINI_API_KEY."

    compact = [
        {
            "title": v["title"],
            "url": v["url"],
            "views": v["views"],
            "likes": v["likes"],
            "comments": v["comments"],
            "shares": v["shares"]
        }
        for v in videos[:30]
    ]

    prompt = f"""
Ты — аналитик мотоциклетного рынка и контента.
Проанализируй список TikTok-видео.

Оставляй только темы:
мотоциклы, запчасти, экипировка, тюнинг, ремонт,
аксессуары, мотоновости и мотопутешествия.

Не выдумывай характеристики и статистику.
Выдели:
1. Самые перспективные темы.
2. Интересные товары и запчасти.
3. Идеи для публикаций.
4. Видео с высокой вовлечённостью.
5. Что стоит проверить вручную.

Данные:
{compact}
"""
    try:
        response = await asyncio.to_thread(
            ai.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text or "ИИ не вернул текст."
    except Exception as exc:
        log.exception("Gemini error: %s", exc)
        return f"Не удалось выполнить ИИ-анализ: {exc}"


# ========== СКАНИРОВАНИЕ И ОТЧЁТ ==========

async def send_long_message(chat_id, text):
    limit = 3800
    for start in range(0, len(text), limit):
        await bot.send_message(chat_id, text[start:start + limit])


async def run_scan(chat_id):
    if scan_lock.locked():
        await bot.send_message(chat_id, "Сканирование уже выполняется.")
        return

    async with scan_lock:
        stop_scan.clear()
        started = datetime.now(TIMEZONE)
        await bot.send_message(
            chat_id,
            "🏍 DANIL IRBIS AI SCOUT: начинаю сканирование TikTok."
        )

        try:
            videos = await scan_tiktok()
            new_videos = [v for v in videos if save_video(v)]

            analysis = await analyze_with_ai(new_videos)

            log_run(len(new_videos), "stopped" if stop_scan.is_set() else "ok")

            report = (
                "🏁 СКАНИРОВАНИЕ ЗАВЕРШЕНО\n\n"
                f"Найдено роликов: {len(videos)}\n"
                f"Новых сохранено: {len(new_videos)}\n"
                f"Всего в базе: {count_videos()}\n"
                f"Время: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M')}\n\n"
                "🤖 АНАЛИЗ GEMINI\n"
                f"{analysis}"
            )
            await send_long_message(chat_id, report)

        except RuntimeError as exc:
            if str(exc) == "CAPTCHA":
                log_run(0, "captcha")
                await bot.send_message(
                    chat_id,
                    "⚠️ TikTok показал CAPTCHA или проверку безопасности.\n"
                    "Сканирование остановлено. Пройди проверку вручную "
                    "через /login, затем выполни /scan."
                )
            else:
                log_run(0, "error")
                await bot.send_message(chat_id, f"Ошибка: {exc}")

        except Exception as exc:
            log.exception("Scan failed")
            log_run(0, "error")
            await bot.send_message(chat_id, f"Ошибка сканирования: {exc}")


# ========== TELEGRAM-КОМАНДЫ ==========

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not await admin_only(message):
        return
    await message.answer(
        "🏍 DANIL IRBIS AI SCOUT 2.0\n\n"
        "/login — открыть вход TikTok\n"
        "/login_done — завершить вход и сохранить сессию\n"
        "/scan — начать сканирование\n"
        "/stop — остановить сканирование\n"
        "/top — лучшие сохранённые ролики\n"
        "/status — состояние бота\n"
        "/help — список команд"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await start_cmd(message)


@dp.message(Command("login"))
async def login_cmd(message: Message):
    global login_context, login_playwright

    if not await admin_only(message):
        return

    if login_context:
        await message.answer("Сессия входа уже открыта. Используй /login_done.")
        return

    try:
        login_playwright, login_context = await create_browser()
        page = login_context.pages[0] if login_context.pages else (
            await login_context.new_page()
        )
        await page.goto(
            "https://www.tiktok.com/login",
            wait_until="domcontentloaded",
            timeout=60000
        )
        await asyncio.sleep(5)

        image = await page.screenshot(full_page=True)
        from aiogram.types import BufferedInputFile
        await message.answer_photo(
            BufferedInputFile(image, filename="tiktok_login.png"),
            caption=(
                "Открыл страницу входа TikTok.\n"
                "Если QR-код отображается, отсканируй его в приложении TikTok. "
                "Не отправляй мне пароль или коды подтверждения.\n"
                "После входа напиши /login_done."
            )
        )
    except Exception as exc:
        log.exception("Login error")
        if login_context:
            await login_context.close()
        if login_playwright:
            await login_playwright.stop()
        login_context = None
        login_playwright = None
        await message.answer(f"Не удалось открыть страницу входа: {exc}")


@dp.message(Command("login_done"))
async def login_done_cmd(message: Message):
    global login_context, login_playwright

    if not await admin_only(message):
        return

    if not login_context:
        await message.answer("Сначала запусти /login.")
        return

    try:
        pages = login_context.pages
        if pages:
            page = pages[0]
            await page.goto(
                "https://www.tiktok.com/foryou",
                wait_until="domcontentloaded",
                timeout=30000
            )
            await asyncio.sleep(3)
            if await page_is_captcha(page):
                await message.answer(
                    "TikTok показывает проверку. Пройди её вручную, "
                    "затем снова выполни /login_done."
                )
                return

        await login_context.close()
        await login_playwright.stop()
        login_context = None
        login_playwright = None

        await message.answer(
            "Сессия браузера сохранена в профиле. "
            "Попробуй /scan. Успешность авторизации проверится при сканировании."
        )
    except Exception as exc:
        await message.answer(f"Ошибка сохранения сессии: {exc}")


@dp.message(Command("scan"))
async def scan_cmd(message: Message):
    if not await admin_only(message):
        return
    await run_scan(message.chat.id)


@dp.message(Command("stop"))
async def stop_cmd(message: Message):
    if not await admin_only(message):
        return
    stop_scan.set()
    await message.answer("Остановка запрошена. Текущая операция завершится.")


@dp.message(Command("top"))
async def top_cmd(message: Message):
    if not await admin_only(message):
        return

    rows = top_videos(10)
    if not rows:
        await message.answer("Пока нет сохранённых роликов. Запусти /scan.")
        return

    text = ["🏆 ЛУЧШИЕ РОЛИКИ"]
    for i, row in enumerate(rows, 1):
        title, url, views, likes, comments, shares = row
        text.append(
            f"\n{i}. {title[:180]}\n"
            f"Просмотры: {views} | Лайки: {likes} | "
            f"Комментарии: {comments} | Репосты: {shares}\n{url}"
        )
    await send_long_message(message.chat.id, "\n".join(text))


@dp.message(Command("status"))
async def status_cmd(message: Message):
    if not await admin_only(message):
        return

    running = scan_lock.locked()
    await message.answer(
        "📊 СТАТУС DANIL IRBIS\n\n"
        f"Сканирование: {'работает' if running else 'не запущено'}\n"
        f"Всего видео в базе: {count_videos()}\n"
        f"Профиль TikTok: {'есть' if os.path.isdir(PROFILE_DIR) else 'ещё не создан'}\n"
        f"Gemini: {'подключён' if ai else 'не настроен'}\n"
        f"Часовой пояс: Europe/Moscow\n"
        f"Автозапуск: ежедневно в {DAILY_HOUR:02d}:{DAILY_MINUTE:02d}"
    )


# ========== АВТОЗАПУСК ==========

async def scheduled_scan():
    await run_scan(ADMIN_ID)


async def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("ВСТАВЬ_"):
        raise RuntimeError("Вставь новый Telegram BOT_TOKEN в bot.py")

    db_init()

    scheduler.add_job(
        scheduled_scan,
        trigger="cron",
        hour=DAILY_HOUR,
        minute=DAILY_MINUTE,
        timezone=TIMEZONE,
        id="daily_moto_scan",
        replace_existing=True,
        max_instances=1
    )
    scheduler.start()

    log.info("DANIL IRBIS запущен")
    await bot.send_message(ADMIN_ID, "🏍 DANIL IRBIS AI SCOUT 2.0 запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
