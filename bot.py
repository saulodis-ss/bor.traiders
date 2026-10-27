"""
Telegram-бот сигналов по валютным парам (RSI + MACD).
Котировки: Twelve Data. Библиотеки: aiogram 3 и aiohttp.

Запуск на телефоне: впиши токен и ключ ниже (вместо текста в кавычках).
Запуск на сервере: задай переменные окружения BOT_TOKEN и TD_API_KEY.
"""
import asyncio
import json
import logging
import os
import time

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

TOKEN = os.getenv("BOT_TOKEN", "ТОКЕН_БОТА")
API_KEY = os.getenv("TD_API_KEY", "КЛЮЧ_TWELVE_DATA")

# ---------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------
PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/AUD", "EUR/CAD", "EUR/NZD",
    "GBP/JPY", "GBP/CHF", "GBP/AUD", "GBP/CAD", "GBP/NZD",
    "AUD/JPY", "AUD/CAD", "AUD/CHF", "AUD/NZD",
    "CAD/JPY", "CAD/CHF", "CHF/JPY",
    "NZD/JPY", "NZD/CAD", "NZD/CHF",
]
MAJORS = PAIRS[:7]
MAX_PAIRS = 8  # максимум пар на одного пользователя (из-за лимитов API)

# ключ: (интервал Twelve Data, во сколько раз укрупнять свечи, секунд в свече, название)
# Twelve Data не отдаёт 2, 3 и 10 минут, поэтому они собираются из 1 и 5 минут.
TF = {
    "1m": ("1min", 1, 60, "1 мин"),
    "2m": ("1min", 2, 120, "2 мин"),
    "3m": ("1min", 3, 180, "3 мин"),
    "5m": ("5min", 1, 300, "5 мин"),
    "10m": ("5min", 2, 600, "10 мин"),
    "15m": ("15min", 1, 900, "15 мин"),
    "30m": ("30min", 1, 1800, "30 мин"),
    "1h": ("1h", 1, 3600, "1 час"),
    "4h": ("4h", 1, 14400, "4 часа"),
}
DEFAULT_TF = "15m"
OLD_TF = {"5min": "5m", "15min": "15m"}  # ключи из прошлой версии бота

API_GAP = 8.0       # пауза между запросами (бесплатно: 8 запросов в минуту)
DAILY_LIMIT = 780   # бесплатно: 800 запросов в сутки, оставляем запас

# На Railway при подключённом Volume папка берётся из RAILWAY_VOLUME_MOUNT_PATH,
# иначе файлы лежат рядом с ботом (на телефоне).
DATA_DIR = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")

logging.basicConfig(level=logging.INFO)
DISCLAIMER = "⚠️ Это не финансовый совет. Торговля несёт риск потери денег."

BTN_CHECK = "📊 Сигналы сейчас"
BTN_SETTINGS = "⚙️ Параметры"
BTN_PROFILE = "👤 Профиль"
BTN_STATS = "📈 Статистика"
BTN_BACKTEST = "🧪 Проверка на истории"
BTN_HELP = "ℹ️ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CHECK), KeyboardButton(text=BTN_STATS)],
        [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_BACKTEST), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

api_lock = None  # создаётся в main()
api_state = {"last": 0.0, "day": "", "count": 0}
cache = {}
checking = set()


# ---------------------------------------------------------------
# Хранение данных
# ---------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


users = load_json(USERS_FILE, {})
signals = load_json(SIGNALS_FILE, [])


def save_users():
    save_json(USERS_FILE, users)


def save_signals():
    save_json(SIGNALS_FILE, signals)


def get_user(tg_user):
    key = str(tg_user.id)
    new = key not in users
    if new:
        users[key] = {"joined": time.strftime("%d.%m.%Y")}
    u = users[key]
    u.setdefault("pairs", ["EUR/USD"])
    u.setdefault("tf", DEFAULT_TF)
    u.setdefault("auto", False)
    u.setdefault("strict", False)
    u.setdefault("received", 0)
    u.setdefault("checks", 0)
    u.setdefault("joined", "неизвестно")
    u["tf"] = OLD_TF.get(u["tf"], u["tf"])
    if u["tf"] not in TF:
        u["tf"] = DEFAULT_TF
    u["pairs"] = [p for p in u["pairs"] if p in PAIRS][:MAX_PAIRS]
    u["name"] = tg_user.full_name
    u["username"] = tg_user.username
    if new:
        save_users()
    return u


# ---------------------------------------------------------------
# Индикаторы
# ---------------------------------------------------------------
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_value(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def rsi_series(closes, period=14):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    avg_g = sum(gains[1:period + 1]) / period
    avg_l = sum(losses[1:period + 1]) / period
    out[period] = rsi_value(avg_g, avg_l)
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i] = rsi_value(avg_g, avg_l)
    return out


def macd_hist_series(closes):
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    macd = [a - b for a, b in zip(fast, slow)]
    sig = ema(macd, 9)
    return [m - s for m, s in zip(macd, sig)]


def signal_at(rsis, hist, i, strict=False):
    """Сигнал на свече i: 'BUY', 'SELL' или None."""
    r = rsis[i]
    if r is None or i < 1:
        return None
    h0, h1 = hist[i - 1], hist[i]
    buy_rsi = r < 30
    sell_rsi = r > 70
    if strict:
        # строго: RSI в крайней зоне и MACD уже разворачивается в ту же сторону
        if buy_rsi and h1 > h0:
            return "BUY"
        if sell_rsi and h1 < h0:
            return "SELL"
        return None
    cross_up = h0 <= 0 < h1
    cross_dn = h0 >= 0 > h1
    score = (1 if buy_rsi else 0) + (1 if cross_up else 0)
    score -= (1 if sell_rsi else 0) + (1 if cross_dn else 0)
    if score > 0:
        return "BUY"
    if score < 0:
        return "SELL"
    return None


def build_text(pair, tf, closes, rsis, hist, side):
    i = len(closes) - 1
    rsi = rsis[i]
    if rsi < 30:
        rsi_line = f"RSI {rsi:.1f}: перепроданность"
    elif rsi > 70:
        rsi_line = f"RSI {rsi:.1f}: перекупленность"
    else:
        rsi_line = f"RSI {rsi:.1f}"
    if hist[i - 1] <= 0 < hist[i]:
        macd_line = "MACD пересёк сигнальную линию вверх"
    elif hist[i - 1] >= 0 > hist[i]:
        macd_line = "MACD пересёк сигнальную линию вниз"
    elif hist[i] > hist[i - 1]:
        macd_line = "MACD: гистограмма растёт"
    else:
        macd_line = "MACD: гистограмма падает"
    title = "🟢 ПОКУПКА" if side == "BUY" else "🔴 ПРОДАЖА"
    return (
        f"{title}\n"
        f"{pair} · срок {TF[tf][3]} · цена {closes[i]}\n"
        f"{rsi_line}\n{macd_line}\n\n{DISCLAIMER}"
    )


def backtest(closes, strict):
    rsis = rsi_series(closes)
    hist = macd_hist_series(closes)
    total = wins = buys = sells = 0
    prev = None
    for i in range(40, len(closes) - 1):
        side = signal_at(rsis, hist, i, strict)
        if side and side != prev:
            total += 1
            if side == "BUY":
                buys += 1
                ok = closes[i + 1] > closes[i]
            else:
                sells += 1
                ok = closes[i + 1] < closes[i]
            if ok:
                wins += 1
        prev = side
    return total, wins, buys, sells


# ---------------------------------------------------------------
# Котировки Twelve Data
# ---------------------------------------------------------------
async def api_get(params):
    async with api_lock:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if api_state["day"] != today:
            api_state["day"] = today
            api_state["count"] = 0
        if api_state["count"] >= DAILY_LIMIT:
            raise ValueError("дневной лимит запросов к Twelve Data исчерпан")
        wait = API_GAP - (time.time() - api_state["last"])
        if wait > 0:
            await asyncio.sleep(wait)
        api_state["last"] = time.time()
        api_state["count"] += 1
        p = dict(params)
        p["apikey"] = API_KEY
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.twelvedata.com/time_series", params=p
            ) as resp:
                return await resp.json()


async def get_closes(pair, tf, need=120):
    interval, k, seconds, _ = TF[tf]
    key = (pair, tf, need)
    hit = cache.get(key)
    if hit and time.time() - hit[0] < min(seconds, 300):
        return hit[1]
    data = await api_get(
        {"symbol": pair, "interval": interval, "outputsize": min(5000, need * k)}
    )
    values = data.get("values")
    if not values:
        raise ValueError(data.get("message", "нет данных"))
    closes = [float(v["close"]) for v in reversed(values)]  # от старых к новым
    if k > 1:
        closes = closes[::-1][::k][::-1]  # укрупняем свечи
    if len(closes) < 40:
        raise ValueError("слишком мало данных")
    cache[key] = (time.time(), closes)
    evaluate_pending(pair, tf, closes[-1])
    return closes


# ---------------------------------------------------------------
# Журнал сигналов и статистика
# ---------------------------------------------------------------
def log_signal(pair, tf, side, price):
    now = time.time()
    for rec in signals[-200:]:
        if (
            rec["pair"] == pair
            and rec["tf"] == tf
            and rec["side"] == side
            and now - rec["ts"] < TF[tf][2]
        ):
            return
    signals.append(
        {"ts": now, "pair": pair, "tf": tf, "side": side, "price": price, "result": None}
    )
    del signals[:-1000]
    save_signals()


def evaluate_pending(pair, tf, price):
    now = time.time()
    changed = False
    for rec in signals:
        if (
            rec["result"] is None
            and rec["pair"] == pair
            and rec["tf"] == tf
            and now - rec["ts"] >= TF[tf][2]
        ):
            win = price > rec["price"] if rec["side"] == "BUY" else price < rec["price"]
            rec["result"] = "win" if win else "loss"
            changed = True
    if changed:
        save_signals()


def stats_text(u):
    done = [r for r in signals if r["result"] in ("win", "loss")]
    wins = sum(1 for r in done if r["result"] == "win")
    pending = sum(1 for r in signals if r["result"] is None)
    lines = [
        "📈 Статистика",
        "",
        "Ты:",
        f"• получено сигналов: {u['received']}",
        f"• запросов проверки: {u['checks']}",
        "",
        "Бот (все сигналы):",
        f"• всего: {len(signals)}",
        f"• проверено: {len(done)}, ждут проверки: {pending}",
    ]
    if done:
        rate = wins / len(done) * 100
        lines.append(f"• верных: {wins} ({rate:.0f}%), неверных: {len(done) - wins}")
    else:
        lines.append("• проверенных сигналов пока нет")
    last = [r for r in signals if r["result"] in ("win", "loss")][-5:]
    if last:
        lines += ["", "Последние проверенные:"]
        for r in reversed(last):
            mark = "✅" if r["result"] == "win" else "❌"
            lines.append(f"{mark} {r['side']} {r['pair']} · {TF[r['tf']][3]}")
    lines += [
        "",
        f"Пользователей в боте: {len(users)}",
        "",
        "Сигнал считается верным, если через один срок цена ушла в нужную "
        "сторону. Проверка грубая, по ближайшему сканированию. Для бинарных "
        "опционов безубыточность обычно около 54–55%.",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------
# Тексты и клавиатуры
# ---------------------------------------------------------------
def mode_name(u):
    return "автоматически" if u["auto"] else "по запросу"


def sens_name(u):
    return "строгая" if u["strict"] else "обычная"


def grid(buttons, cols):
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def settings_text(u):
    pairs = ", ".join(u["pairs"]) if u["pairs"] else "не выбраны"
    return (
        "⚙️ Параметры\n\n"
        f"💱 Пары ({len(u['pairs'])}/{MAX_PAIRS}): {pairs}\n"
        f"⏱ Таймфрейм: {TF[u['tf']][3]}\n"
        f"🔔 Режим: {mode_name(u)}\n"
        f"🎯 Чувствительность: {sens_name(u)}"
    )


def settings_kb(u):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💱 Валютные пары ({len(u['pairs'])})", callback_data="s:pairs")],
            [InlineKeyboardButton(text=f"⏱ Таймфрейм: {TF[u['tf']][3]}", callback_data="s:tf")],
            [InlineKeyboardButton(text=f"🔔 Режим: {mode_name(u)}", callback_data="s:mode")],
            [InlineKeyboardButton(text=f"🎯 Чувствительность: {sens_name(u)}", callback_data="s:sens")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="s:close")],
        ]
    )


def back_row():
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data="s:menu")]


def pairs_text(u):
    return (
        f"💱 Валютные пары ({len(u['pairs'])}/{MAX_PAIRS})\n"
        "Нажми на пару, чтобы включить или выключить её."
    )


def pairs_kb(u):
    btns = [
        InlineKeyboardButton(
            text=("✅ " if p in u["pairs"] else "") + p, callback_data="p:" + p
        )
        for p in PAIRS
    ]
    rows = grid(btns, 3)
    rows.append(
        [
            InlineKeyboardButton(text="⭐ Основные", callback_data="p:majors"),
            InlineKeyboardButton(text="🧹 Очистить", callback_data="p:clear"),
        ]
    )
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


TF_TEXT = (
    "⏱ Таймфрейм\n"
    "Это и период свечи, и срок сделки.\n\n"
    "Короткие таймфреймы (1–3 мин) в автоматическом режиме быстро "
    "расходуют дневной лимит бесплатного Twelve Data (800 запросов)."
)


def tf_kb(u):
    btns = [
        InlineKeyboardButton(
            text=("✅ " if u["tf"] == key else "") + TF[key][3], callback_data="t:" + key
        )
        for key in TF
    ]
    rows = grid(btns, 3)
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mode_kb(u):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=("✅ " if u["auto"] else "") + "Автоматически", callback_data="m:auto")],
            [InlineKeyboardButton(text=("" if u["auto"] else "✅ ") + "По запросу", callback_data="m:manual")],
            back_row(),
        ]
    )


MODE_TEXT = (
    "🔔 Режим сигналов\n\n"
    "Автоматически: бот сам пришлёт сигнал, когда он появится.\n"
    "По запросу: сигналы только по кнопке «📊 Сигналы сейчас»."
)


def sens_kb(u):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=("" if u["strict"] else "✅ ") + "Обычная", callback_data="x:normal")],
            [InlineKeyboardButton(text=("✅ " if u["strict"] else "") + "Строгая", callback_data="x:strict")],
            back_row(),
        ]
    )


SENS_TEXT = (
    "🎯 Чувствительность\n\n"
    "Обычная: сигнал, если сработал RSI или MACD. Сигналов больше, но больше и ложных.\n"
    "Строгая: нужен RSI в крайней зоне и разворот MACD в ту же сторону. "
    "Сигналов меньше."
)


def profile_text(tg_user, u):
    nick = "@" + tg_user.username if tg_user.username else "не указан"
    pairs = ", ".join(u["pairs"]) if u["pairs"] else "не выбраны"
    return (
        "👤 Профиль\n\n"
        f"Имя: {tg_user.full_name}\n"
        f"Ник: {nick}\n"
        f"ID: {tg_user.id}\n"
        f"В боте с: {u['joined']}\n\n"
        f"Пары: {pairs}\n"
        f"Таймфрейм: {TF[u['tf']][3]}\n"
        f"Режим: {mode_name(u)}\n"
        f"Чувствительность: {sens_name(u)}\n\n"
        f"Получено сигналов: {u['received']}"
    )


HELP = (
    "ℹ️ Как пользоваться\n\n"
    "📊 Сигналы сейчас: проверить выбранные пары прямо сейчас\n"
    "⚙️ Параметры: пары, таймфрейм, режим, чувствительность\n"
    "👤 Профиль: твои данные и настройки\n"
    "📈 Статистика: сколько сигналов и сколько из них сработало\n"
    "🧪 Проверка на истории: как правила сработали бы на прошлых данных\n\n"
    "Команды: /start /check /settings /profile /stats /backtest /help\n\n"
    + DISCLAIMER
)


async def edit(cb, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # например, текст не изменился


# ---------------------------------------------------------------
# Команды и кнопки главного меню
# ---------------------------------------------------------------
@dp.message(CommandStart())
async def start(message: Message):
    get_user(message.from_user)
    await message.answer(
        f"Привет, {message.from_user.full_name}! 👋\n"
        "Я присылаю сигналы по валютным парам на основе RSI и MACD.\n\n"
        "Начни с «⚙️ Параметры»: выбери пары, таймфрейм и режим. "
        "Потом нажми «📊 Сигналы сейчас» или включи автоматический режим.\n\n"
        + DISCLAIMER,
        reply_markup=MAIN_KB,
    )


@dp.message(Command("menu"))
async def menu_cmd(message: Message):
    await message.answer("Главное меню 👇", reply_markup=MAIN_KB)


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def help_cmd(message: Message):
    await message.answer(HELP, reply_markup=MAIN_KB)


@dp.message(Command("settings"))
@dp.message(F.text == BTN_SETTINGS)
async def settings_cmd(message: Message):
    u = get_user(message.from_user)
    await message.answer(settings_text(u), reply_markup=settings_kb(u))


@dp.message(Command("profile"))
@dp.message(F.text == BTN_PROFILE)
async def profile_cmd(message: Message):
    u = get_user(message.from_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Изменить параметры", callback_data="s:menu")]
        ]
    )
    await message.answer(profile_text(message.from_user, u), reply_markup=kb)


@dp.message(Command("stats"))
@dp.message(F.text == BTN_STATS)
async def stats_cmd(message: Message):
    u = get_user(message.from_user)
    await message.answer(stats_text(u))


@dp.message(Command("check"))
@dp.message(F.text == BTN_CHECK)
async def check_cmd(message: Message):
    u = get_user(message.from_user)
    uid = str(message.from_user.id)
    if not u["pairs"]:
        await message.answer("Сначала выбери пары в «⚙️ Параметры».")
        return
    if uid in checking:
        await message.answer("Я уже считаю сигналы, подожди немного.")
        return
    checking.add(uid)
    try:
        tf = u["tf"]
        await message.answer(
            f"Считаю пар: {len(u['pairs'])}, таймфрейм {TF[tf][3]}. "
            "Это может занять до минуты..."
        )
        u["checks"] += 1
        quiet = []
        found = 0
        for pair in list(u["pairs"]):
            try:
                closes = await get_closes(pair, tf)
            except Exception as e:
                await message.answer(f"{pair}: не удалось получить данные ({e})")
                continue
            rsis = rsi_series(closes)
            hist = macd_hist_series(closes)
            i = len(closes) - 1
            side = signal_at(rsis, hist, i, u["strict"])
            if side:
                found += 1
                log_signal(pair, tf, side, closes[i])
                u["received"] += 1
                await message.answer(build_text(pair, tf, closes, rsis, hist, side))
            else:
                quiet.append(f"{pair} (RSI {rsis[i]:.0f})")
        save_users()
        if quiet:
            head = "⚪ Без сигнала:" if found else "⚪ Сейчас сигналов нет:"
            await message.answer(head + " " + ", ".join(quiet))
    finally:
        checking.discard(uid)


@dp.message(Command("backtest"))
@dp.message(F.text == BTN_BACKTEST)
async def backtest_cmd(message: Message):
    u = get_user(message.from_user)
    if not u["pairs"]:
        await message.answer("Сначала выбери пары в «⚙️ Параметры».")
        return
    btns = [InlineKeyboardButton(text=p, callback_data="b:" + p) for p in u["pairs"]]
    await message.answer(
        f"🧪 Проверка на истории\nТаймфрейм: {TF[u['tf']][3]}, "
        f"чувствительность: {sens_name(u)}.\nВыбери пару:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=grid(btns, 3)),
    )


# ---------------------------------------------------------------
# Кнопки под сообщениями
# ---------------------------------------------------------------
@dp.callback_query(F.data.startswith("s:"))
async def settings_nav(cb: CallbackQuery):
    u = get_user(cb.from_user)
    action = cb.data[2:]
    if action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
    elif action == "menu":
        await edit(cb, settings_text(u), settings_kb(u))
    elif action == "pairs":
        await edit(cb, pairs_text(u), pairs_kb(u))
    elif action == "tf":
        await edit(cb, TF_TEXT, tf_kb(u))
    elif action == "mode":
        await edit(cb, MODE_TEXT, mode_kb(u))
    elif action == "sens":
        await edit(cb, SENS_TEXT, sens_kb(u))
    await cb.answer()


@dp.callback_query(F.data.startswith("p:"))
async def pick_pair(cb: CallbackQuery):
    u = get_user(cb.from_user)
    value = cb.data[2:]
    if value == "clear":
        u["pairs"] = []
    elif value == "majors":
        u["pairs"] = list(MAJORS)
    elif value in PAIRS:
        if value in u["pairs"]:
            u["pairs"].remove(value)
        elif len(u["pairs"]) >= MAX_PAIRS:
            await cb.answer(f"Максимум {MAX_PAIRS} пар", show_alert=True)
            return
        else:
            u["pairs"].append(value)
    save_users()
    await edit(cb, pairs_text(u), pairs_kb(u))
    await cb.answer()


@dp.callback_query(F.data.startswith("t:"))
async def pick_tf(cb: CallbackQuery):
    u = get_user(cb.from_user)
    key = cb.data[2:]
    if key not in TF:
        await cb.answer("Эта кнопка устарела")
        return
    u["tf"] = key
    save_users()
    await edit(cb, TF_TEXT, tf_kb(u))
    await cb.answer("Таймфрейм обновлён")


@dp.callback_query(F.data.startswith("m:"))
async def pick_mode(cb: CallbackQuery):
    u = get_user(cb.from_user)
    u["auto"] = cb.data[2:] == "auto"
    save_users()
    await edit(cb, MODE_TEXT, mode_kb(u))
    if u["auto"] and TF[u["tf"]][2] <= 180:
        await cb.answer(
            "Короткие таймфреймы быстро исчерпывают дневной лимит бесплатного API",
            show_alert=True,
        )
    else:
        await cb.answer("Режим обновлён")


@dp.callback_query(F.data.startswith("x:"))
async def pick_sens(cb: CallbackQuery):
    u = get_user(cb.from_user)
    u["strict"] = cb.data[2:] == "strict"
    save_users()
    await edit(cb, SENS_TEXT, sens_kb(u))
    await cb.answer("Чувствительность обновлена")


@dp.callback_query(F.data.startswith("b:"))
async def backtest_run(cb: CallbackQuery):
    u = get_user(cb.from_user)
    pair = cb.data[2:]
    await cb.answer()
    if pair not in PAIRS:
        return
    tf = u["tf"]
    await cb.message.answer("Загружаю историю, подожди несколько секунд...")
    try:
        closes = await get_closes(pair, tf, need=800)
    except Exception as e:
        await cb.message.answer(f"Не удалось получить историю ({e})")
        return
    total, wins, buys, sells = backtest(closes, u["strict"])
    if total == 0:
        body = "За этот период сигналов не было."
    else:
        rate = wins / total * 100
        body = (
            f"Сигналов: {total} (покупка {buys}, продажа {sells})\n"
            f"Верных: {wins} из {total} ({rate:.0f}%)"
        )
    await cb.message.answer(
        f"🧪 Проверка на истории\n{pair} · {TF[tf][3]} · свечей: {len(closes)}\n\n"
        f"{body}\n\n"
        "Сигнал считается верным, если следующая свеча закрылась в нужную "
        "сторону. Спред, комиссии и выплата брокера не учтены, а на новых "
        "данных результат обычно хуже.\n\n" + DISCLAIMER
    )


# ---------------------------------------------------------------
# Автоматические сигналы
# ---------------------------------------------------------------
async def auto_tick(last_fetch, last_signal):
    needed = {}
    for uid, u in users.items():
        if u.get("auto"):
            tf = OLD_TF.get(u.get("tf"), u.get("tf"))
            if tf not in TF:
                continue
            for pair in u.get("pairs", []):
                if pair in PAIRS:
                    needed.setdefault((pair, tf), []).append(uid)

    now = time.time()
    due = [
        (last_fetch.get(key, 0), key)
        for key in needed
        if now - last_fetch.get(key, 0) >= TF[key[1]][2]
    ]
    due.sort()
    for _, key in due:
        pair, tf = key
        last_fetch[key] = time.time()
        try:
            closes = await get_closes(pair, tf)
        except Exception as e:
            print("auto: ошибка данных", pair, tf, e)
            continue
        rsis = rsi_series(closes)
        hist = macd_hist_series(closes)
        i = len(closes) - 1
        for strict in (False, True):
            side = signal_at(rsis, hist, i, strict)
            skey = (pair, tf, strict)
            prev = last_signal.get(skey)
            last_signal[skey] = side
            if side is None or side == prev:
                continue
            log_signal(pair, tf, side, closes[i])
            text = build_text(pair, tf, closes, rsis, hist, side)
            for uid in needed[key]:
                u = users.get(uid)
                if u and u.get("auto") and bool(u.get("strict")) == strict:
                    try:
                        await bot.send_message(int(uid), text)
                        u["received"] = u.get("received", 0) + 1
                    except Exception:
                        pass  # например, пользователь заблокировал бота
        save_users()


async def auto_loop():
    last_fetch = {}
    last_signal = {}
    while True:
        try:
            await auto_tick(last_fetch, last_signal)
        except Exception as e:
            print("auto_loop ошибка:", e)
        await asyncio.sleep(10)


async def main():
    global api_lock
    api_lock = asyncio.Lock()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="check", description="Сигналы сейчас"),
            BotCommand(command="settings", description="Параметры"),
            BotCommand(command="profile", description="Профиль"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="backtest", description="Проверка на истории"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    asyncio.create_task(auto_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())