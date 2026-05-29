"""
Instagram Promotion Telegram Bot — v3.0
========================================
Новые возможности:
  ✅ Несколько Instagram-аккаунтов на одного Telegram-пользователя
  ✅ Переключение между аккаунтами через /switch
  ✅ Добавление аккаунта через кнопку "Добавить аккаунт"
  ✅ Хранение и авто-загрузка сессии instagrapi
  ✅ Чёрный список — не подписываемся повторно
  ✅ Умные отписки — только те, кто не подписался в ответ за N дней
  ✅ Фильтрация ботов (проверка числа постов и дат активности)
  ✅ Ротация пауз по времени суток (активность только 9:00–22:00)
  ✅ Жёсткий дневной лимит (защита от бана Instagram)

Установка:
    pip install python-telegram-bot instagrapi

Запуск:
    python bot.py
"""

import sqlite3
import logging
import random
import asyncio
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ──────────────────────────────────────────────
# КОНФИГ
# ──────────────────────────────────────────────
from dotenv import load_dotenv
import os
load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле!")
DB_PATH = "bot.db"
SESSIONS_DIR = Path("sessions")     # папка для хранения сессий Instagram
SESSIONS_DIR.mkdir(exist_ok=True)

# Рабочие часы (по времени сервера, UTC+5 для Ташкента)
WORK_HOUR_START = 9    # 09:00
WORK_HOUR_END   = 22   # 22:00

# Паузы между действиями (секунды)
MIN_PAUSE = 35
MAX_PAUSE = 130

# Дневные лимиты (жёстко — Instagram банит за превышение)
DAILY_LIMITS = {
    "subscribe":   150,
    "unsubscribe": 150,
    "like":        300,
}

# Сколько дней ждать взаимной подписки перед отпиской
UNFOLLOW_AFTER_DAYS = 3

# Минимальные признаки "живого" аккаунта (фильтр ботов)
MIN_POSTS       = 3     # хотя бы 3 поста
MAX_FOLLOWINGS  = 5000  # не массфолловер
MIN_FOLLOWERS   = 10    # не совсем пустой


# ──────────────────────────────────────────────
# СОСТОЯНИЯ ДИАЛОГОВ
# ──────────────────────────────────────────────
(
    # Добавление/регистрация аккаунта
    ADD_LOGIN,
    ADD_PASSWORD,
    # Выбор целевого аккаунта
    WAITING_TARGET,
    # Подтверждение плана
    WAITING_CONFIRM,
    # Переключение аккаунта
    SWITCH_ACCOUNT,
) = range(5)

# ──────────────────────────────────────────────
# ЛОГИРОВАНИЕ
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# БАЗА ДАННЫХ
# ══════════════════════════════════════════════

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- Telegram-пользователи
        CREATE TABLE IF NOT EXISTS tg_users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            active_account_id INTEGER,   -- текущий активный аккаунт Instagram
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Instagram-аккаунты (несколько на одного Telegram-пользователя)
        CREATE TABLE IF NOT EXISTS ig_accounts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            ig_login        TEXT NOT NULL,
            password_hash   TEXT NOT NULL,
            session_file    TEXT,
            added_at        TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, ig_login),
            FOREIGN KEY (user_id) REFERENCES tg_users(user_id)
        );

        -- Дневные планы (привязаны к ig_account)
        CREATE TABLE IF NOT EXISTS daily_plans (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id          INTEGER NOT NULL,
            day_number          INTEGER NOT NULL,
            target_account      TEXT NOT NULL,
            subscribes_plan     INTEGER DEFAULT 0,
            unsubscribes_plan   INTEGER DEFAULT 0,
            likes_plan          INTEGER DEFAULT 0,
            subscribes_done     INTEGER DEFAULT 0,
            unsubscribes_done   INTEGER DEFAULT 0,
            likes_done          INTEGER DEFAULT 0,
            plan_date           TEXT DEFAULT (date('now')),
            FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
        );

        -- Журнал действий
        CREATE TABLE IF NOT EXISTS action_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      INTEGER NOT NULL,
            action_type     TEXT NOT NULL,   -- subscribe / unsubscribe / like
            target_ig_id    TEXT NOT NULL,   -- Instagram user_id цели
            target_username TEXT,
            status          TEXT DEFAULT 'ok',
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
        );

        -- Чёрный список (уже обработанные, не трогаем снова)
        CREATE TABLE IF NOT EXISTS blacklist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL,
            target_ig_id TEXT NOT NULL,
            reason      TEXT,        -- 'subscribed', 'unsubscribed', 'bot_filtered'
            added_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, target_ig_id),
            FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
        );
        """)
    logger.info("БД инициализирована.")


# ──────────────────────────────────────────────
# Хелперы БД
# ──────────────────────────────────────────────

def hash_pwd(pw: str) -> str:
    return sha256(pw.encode()).hexdigest()


def get_tg_user(user_id: int):
    with get_conn() as c:
        return c.execute("SELECT * FROM tg_users WHERE user_id=?", (user_id,)).fetchone()


def get_active_account(user_id: int):
    """Возвращает активный Instagram-аккаунт пользователя."""
    with get_conn() as c:
        row = c.execute(
            "SELECT active_account_id FROM tg_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row or not row["active_account_id"]:
            return None
        return c.execute(
            "SELECT * FROM ig_accounts WHERE id=?", (row["active_account_id"],)
        ).fetchone()


def get_all_accounts(user_id: int) -> list:
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM ig_accounts WHERE user_id=? ORDER BY added_at", (user_id,)
        ).fetchall()


def set_active_account(user_id: int, account_id: int):
    with get_conn() as c:
        c.execute(
            "UPDATE tg_users SET active_account_id=? WHERE user_id=?",
            (account_id, user_id),
        )


def add_to_blacklist(account_id: int, target_ig_id: str, reason: str):
    with get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO blacklist (account_id, target_ig_id, reason) VALUES (?,?,?)",
            (account_id, target_ig_id, reason),
        )


def is_blacklisted(account_id: int, target_ig_id: str) -> bool:
    with get_conn() as c:
        row = c.execute(
            "SELECT 1 FROM blacklist WHERE account_id=? AND target_ig_id=?",
            (account_id, target_ig_id),
        ).fetchone()
    return row is not None


def get_daily_done(account_id: int, action_type: str) -> int:
    """Сколько действий данного типа уже сделано сегодня."""
    with get_conn() as c:
        row = c.execute(
            """SELECT COUNT(*) as cnt FROM action_log
               WHERE account_id=? AND action_type=?
               AND date(created_at)=date('now')""",
            (account_id, action_type),
        ).fetchone()
    return row["cnt"] if row else 0


def get_current_day(account_id: int) -> int:
    with get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) as cnt FROM daily_plans WHERE account_id=?", (account_id,)
        ).fetchone()
    return (row["cnt"] % 7) + 1


def log_action(account_id: int, action_type: str, target_ig_id: str,
               target_username: str = "", status: str = "ok"):
    with get_conn() as c:
        c.execute(
            """INSERT INTO action_log
               (account_id, action_type, target_ig_id, target_username, status)
               VALUES (?,?,?,?,?)""",
            (account_id, action_type, target_ig_id, target_username, status),
        )


def get_unresponded_follows(account_id: int) -> list:
    """
    Возвращает список target_ig_id, на кого мы подписались
    более UNFOLLOW_AFTER_DAYS дней назад, но они не подписались в ответ.
    (Реализация на уровне нашей БД — мы не проверяем Instagram live,
     это делается отдельно в RealInstagramClient.get_my_followings_not_following_back)
    """
    cutoff = (datetime.now() - timedelta(days=UNFOLLOW_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        rows = c.execute(
            """SELECT target_ig_id FROM action_log
               WHERE account_id=? AND action_type='subscribe'
               AND status='ok' AND created_at <= ?
               AND target_ig_id NOT IN (
                   SELECT target_ig_id FROM action_log
                   WHERE account_id=? AND action_type='unsubscribe'
               )""",
            (account_id, cutoff, account_id),
        ).fetchall()
    return [r["target_ig_id"] for r in rows]


def calculate_plan(day_number: int) -> dict:
    if 1 <= day_number <= 6:
        return {"subscribes": 25, "unsubscribes": 10, "likes": 15}
    return {"subscribes": 10, "unsubscribes": 30, "likes": 5}


# ══════════════════════════════════════════════
# INSTAGRAM КЛИЕНТ
# ══════════════════════════════════════════════

def make_client(session_file: str = None):
    """Создаёт RealInstagramClient или MockInstagramClient."""
    try:
        import instagrapi  # noqa
        return RealInstagramClient(session_file)
    except ImportError:
        logger.warning("instagrapi не найден → используется Mock-клиент")
        return MockInstagramClient()


class RealInstagramClient:
    def __init__(self, session_file: str = None):
        from instagrapi import Client
        self.cl = Client()
        self.cl.delay_range = [MIN_PAUSE, MAX_PAUSE]
        if session_file and Path(session_file).exists():
            try:
                self.cl.load_settings(session_file)
                logger.info(f"Сессия загружена: {session_file}")
            except Exception as e:
                logger.warning(f"Не удалось загрузить сессию {session_file}: {e}")

    def login(self, login: str, password: str, session_file: str) -> dict:
        try:
            self.cl.login(login, password)
            self.cl.dump_settings(session_file)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def relogin(self, login: str, password: str, session_file: str) -> bool:
        """Повторный вход при протухшей сессии."""
        result = self.login(login, password, session_file)
        return result["ok"]

    def is_real_user(self, user_info) -> bool:
        """Проверяет, что аккаунт не бот."""
        try:
            media_count = getattr(user_info, "media_count", 0) or 0
            follower_count = getattr(user_info, "follower_count", 0) or 0
            following_count = getattr(user_info, "following_count", 0) or 0
            return (
                media_count >= MIN_POSTS
                and follower_count >= MIN_FOLLOWERS
                and following_count <= MAX_FOLLOWINGS
                and not getattr(user_info, "is_private", False)  # опционально
            )
        except Exception:
            return False

    def get_filtered_followers(self, target_username: str, amount: int) -> list:
        """
        Возвращает список (user_id, username) живых подписчиков target_username.
        Фильтрует ботов по числу постов и подписок.
        """
        try:
            uid = self.cl.user_id_from_username(target_username)
            raw = self.cl.user_followers(uid, amount=amount * 3)  # берём с запасом
            result = []
            for follower_id, info in raw.items():
                if len(result) >= amount:
                    break
                try:
                    full_info = self.cl.user_info(follower_id)
                    if self.is_real_user(full_info):
                        result.append((str(follower_id), full_info.username))
                except Exception:
                    continue
            return result
        except Exception as e:
            logger.error(f"get_filtered_followers error: {e}")
            return []

    def get_my_followings_not_following_back(self, ig_login: str, candidate_ids: list) -> list:
        """
        Из списка candidate_ids возвращает тех, кто НЕ подписан на наш аккаунт.
        (Умные отписки)
        """
        try:
            my_id = self.cl.user_id_from_username(ig_login)
            my_followers_ids = set(
                str(k) for k in self.cl.user_followers(my_id, amount=5000).keys()
            )
            return [uid for uid in candidate_ids if uid not in my_followers_ids]
        except Exception as e:
            logger.error(f"get_followings_not_following_back error: {e}")
            return candidate_ids  # fallback — отписываемся от всех кандидатов

    def follow(self, user_id: str) -> bool:
        try:
            self.cl.user_follow(int(user_id))
            return True
        except Exception as e:
            logger.error(f"follow {user_id} error: {e}")
            return False

    def unfollow(self, user_id: str) -> bool:
        try:
            self.cl.user_unfollow(int(user_id))
            return True
        except Exception as e:
            logger.error(f"unfollow {user_id} error: {e}")
            return False

    def like_last_post(self, user_id: str) -> bool:
        try:
            medias = self.cl.user_medias(int(user_id), 1)
            if medias:
                self.cl.media_like(medias[0].id)
                return True
            return False
        except Exception as e:
            logger.error(f"like {user_id} error: {e}")
            return False


class MockInstagramClient:
    """Заглушка для работы без реального Instagram (тест/демо)."""

    def login(self, login, password, session_file) -> dict:
        logger.info(f"[MOCK] login: {login}")
        Path(session_file).write_text("{}")
        return {"ok": True}

    def relogin(self, login, password, session_file) -> bool:
        return True

    def get_filtered_followers(self, target_username, amount) -> list:
        logger.info(f"[MOCK] get_filtered_followers: {target_username}, n={amount}")
        return [(f"ig_uid_{i}", f"user_{i}") for i in range(amount)]

    def get_my_followings_not_following_back(self, ig_login, candidate_ids) -> list:
        # В демо — возвращаем половину как "не ответивших"
        return candidate_ids[: len(candidate_ids) // 2]

    def follow(self, user_id) -> bool:
        logger.info(f"[MOCK] follow: {user_id}")
        return True

    def unfollow(self, user_id) -> bool:
        logger.info(f"[MOCK] unfollow: {user_id}")
        return True

    def like_last_post(self, user_id) -> bool:
        logger.info(f"[MOCK] like: {user_id}")
        return True


# ══════════════════════════════════════════════
# УТИЛИТЫ
# ══════════════════════════════════════════════

def is_work_time() -> bool:
    """Проверяет, что сейчас рабочие часы (9:00–22:00)."""
    hour = datetime.now().hour
    return WORK_HOUR_START <= hour < WORK_HOUR_END


async def wait_until_work_time(update: Update):
    """Если сейчас ночь — ждём начала рабочего дня и уведомляем."""
    if not is_work_time():
        now = datetime.now()
        next_start = now.replace(hour=WORK_HOUR_START, minute=0, second=0, microsecond=0)
        if now.hour >= WORK_HOUR_END:
            next_start += timedelta(days=1)
        wait_sec = (next_start - now).total_seconds()
        await update.message.reply_text(
            f"🌙 Сейчас {now.strftime('%H:%M')}. Бот работает только с "
            f"{WORK_HOUR_START}:00 до {WORK_HOUR_END}:00.\n"
            f"Жду до {next_start.strftime('%H:%M')}... (~{int(wait_sec/60)} мин)"
        )
        await asyncio.sleep(wait_sec)


def check_daily_limit(account_id: int, action_type: str) -> tuple[int, int]:
    """Возвращает (уже_сделано, лимит)."""
    done = get_daily_done(account_id, action_type)
    limit = DAILY_LIMITS.get(action_type, 0)
    return done, limit


def main_keyboard(has_accounts: bool) -> ReplyKeyboardMarkup:
    """Главная клавиатура."""
    buttons = [["▶️ Начать день"]]
    if has_accounts:
        buttons.append(["➕ Добавить аккаунт", "🔄 Сменить аккаунт"])
    else:
        buttons.append(["➕ Добавить аккаунт"])
    buttons.append(["📊 Статистика", "❓ Помощь"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# ══════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ / СТАРТ
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    name = update.effective_user.first_name or "друг"

    tg_user = get_tg_user(user_id)

    if not tg_user:
        # Новый пользователь — создаём запись
        with get_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO tg_users (user_id, username) VALUES (?,?)",
                (user_id, update.effective_user.username or ""),
            )
        await update.message.reply_text(
            f"👋 Привет, {name}!\n\n"
            "Я помогу продвигать Instagram-аккаунты.\n"
            "Для начала добавь свой первый аккаунт Instagram.\n\n"
            "📧 Введи логин Instagram:",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data["adding_first"] = True
        return ADD_LOGIN

    accounts = get_all_accounts(user_id)
    if not accounts:
        await update.message.reply_text(
            f"👋 {name}, у тебя нет добавленных аккаунтов.\n"
            "📧 Введи логин Instagram:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ADD_LOGIN

    active = get_active_account(user_id)
    active_str = f"@{active['ig_login']}" if active else "не выбран"

    await update.message.reply_text(
        f"👋 С возвращением, {name}!\n\n"
        f"🟢 Активный аккаунт: {active_str}\n"
        f"📋 Всего аккаунтов: {len(accounts)}\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard(has_accounts=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════
# ДОБАВЛЕНИЕ АККАУНТА
# ══════════════════════════════════════════════

async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запуск через кнопку '➕ Добавить аккаунт'."""
    await update.message.reply_text(
        "➕ *Добавление нового аккаунта*\n\n"
        "📧 Введи логин Instagram:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_LOGIN


async def process_add_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    login = update.message.text.strip().lstrip("@")
    if not login:
        await update.message.reply_text("❗ Логин не может быть пустым:")
        return ADD_LOGIN

    user_id = update.effective_user.id
    # Проверяем, не добавлен ли уже
    with get_conn() as c:
        exists = c.execute(
            "SELECT 1 FROM ig_accounts WHERE user_id=? AND ig_login=?", (user_id, login)
        ).fetchone()
    if exists:
        await update.message.reply_text(
            f"⚠️ Аккаунт @{login} уже добавлен.\n"
            "Введи другой логин или /cancel для отмены:"
        )
        return ADD_LOGIN

    context.user_data["new_ig_login"] = login
    await update.message.reply_text(
        f"✅ Логин: @{login}\n\n"
        "🔒 Введи пароль _(сообщение сразу удалится)_:",
        parse_mode="Markdown",
    )
    return ADD_PASSWORD


async def process_add_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    login = context.user_data.get("new_ig_login")
    user_id = update.effective_user.id

    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text("🔄 Авторизуюсь в Instagram...")

    session_file = str(SESSIONS_DIR / f"session_{user_id}_{login}.json")
    client = make_client()
    result = client.login(login, password, session_file)

    if not result.get("ok"):
        await msg.edit_text(
            f"❌ Ошибка авторизации:\n`{result.get('error', '?')}`\n\n"
            "Попробуй снова: /start",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Сохраняем аккаунт в БД
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO ig_accounts
               (user_id, ig_login, password_hash, session_file)
               VALUES (?,?,?,?)""",
            (user_id, login, hash_pwd(password), session_file),
        )
        account_id = c.execute(
            "SELECT id FROM ig_accounts WHERE user_id=? AND ig_login=?", (user_id, login)
        ).fetchone()["id"]
        # Делаем новый аккаунт активным
        c.execute(
            "UPDATE tg_users SET active_account_id=? WHERE user_id=?",
            (account_id, user_id),
        )

    accounts = get_all_accounts(user_id)
    await msg.edit_text(
        f"🎉 Аккаунт *@{login}* успешно добавлен и выбран как активный!\n\n"
        f"📋 Всего аккаунтов: {len(accounts)}",
        parse_mode="Markdown",
    )

    await update.message.reply_text(
        "Выбери действие:",
        reply_markup=main_keyboard(has_accounts=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════
# ПЕРЕКЛЮЧЕНИЕ АККАУНТА /switch
# ══════════════════════════════════════════════

async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    accounts = get_all_accounts(user_id)

    if not accounts:
        await update.message.reply_text(
            "У тебя нет добавленных аккаунтов. Добавь через кнопку или /start"
        )
        return ConversationHandler.END

    active = get_active_account(user_id)
    active_id = active["id"] if active else None

    # Строим Inline-кнопки для выбора
    buttons = []
    for acc in accounts:
        mark = "🟢 " if acc["id"] == active_id else ""
        buttons.append([
            InlineKeyboardButton(
                f"{mark}@{acc['ig_login']}",
                callback_data=f"switch_{acc['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="switch_cancel")])

    await update.message.reply_text(
        "🔄 *Выбери активный аккаунт:*\n_(🟢 — текущий)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SWITCH_ACCOUNT


async def switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "switch_cancel":
        await query.edit_message_text("Отмена.")
        return ConversationHandler.END

    if data.startswith("switch_"):
        account_id = int(data.split("_")[1])
        with get_conn() as c:
            acc = c.execute("SELECT * FROM ig_accounts WHERE id=?", (account_id,)).fetchone()
        if acc and acc["user_id"] == user_id:
            set_active_account(user_id, account_id)
            await query.edit_message_text(
                f"✅ Активный аккаунт: *@{acc['ig_login']}*",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ Аккаунт не найден.")

    return ConversationHandler.END


# ══════════════════════════════════════════════
# НАЧАЛО ДНЯ — выбор целевого аккаунта
# ══════════════════════════════════════════════

async def start_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    active = get_active_account(user_id)

    if not active:
        await update.message.reply_text(
            "⚠️ Нет активного аккаунта. Добавь через кнопку '➕ Добавить аккаунт'."
        )
        return ConversationHandler.END

    # Проверяем дневные лимиты
    acc_id = active["id"]
    sub_done, sub_limit = check_daily_limit(acc_id, "subscribe")
    if sub_done >= sub_limit:
        await update.message.reply_text(
            f"🚫 Дневной лимит подписок ({sub_limit}) уже достигнут!\n"
            "Возвращайся завтра."
        )
        return ConversationHandler.END

    context.user_data["active_account_id"] = acc_id
    context.user_data["active_login"] = active["ig_login"]

    await update.message.reply_text(
        f"▶️ Активный аккаунт: *@{active['ig_login']}*\n\n"
        "Введи @username аккаунта, у подписчиков которого будем работать:\n"
        "_(Например: @cristiano)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_TARGET


async def process_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.message.text.strip().lstrip("@").strip()
    if not target:
        await update.message.reply_text("❗ Введи корректный @username:")
        return WAITING_TARGET

    acc_id = context.user_data.get("active_account_id")
    context.user_data["target_account"] = target

    day_number = get_current_day(acc_id)
    plan = calculate_plan(day_number)

    # Учитываем уже сделанное сегодня (лимиты)
    sub_done, sub_limit = check_daily_limit(acc_id, "subscribe")
    unsub_done, unsub_limit = check_daily_limit(acc_id, "unsubscribe")
    like_done, like_limit = check_daily_limit(acc_id, "like")

    plan["subscribes"]   = min(plan["subscribes"],   sub_limit - sub_done)
    plan["unsubscribes"] = min(plan["unsubscribes"], unsub_limit - unsub_done)
    plan["likes"]        = min(plan["likes"],         like_limit - like_done)

    context.user_data["plan"] = plan

    # Сохраняем план
    with get_conn() as c:
        c.execute(
            """INSERT INTO daily_plans
               (account_id, day_number, target_account,
                subscribes_plan, unsubscribes_plan, likes_plan)
               VALUES (?,?,?,?,?,?)""",
            (acc_id, day_number, target,
             plan["subscribes"], plan["unsubscribes"], plan["likes"]),
        )
        plan_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    context.user_data["plan_id"] = plan_id

    # Кол-во уже в очереди на отписку
    unfollow_candidates = get_unresponded_follows(acc_id)
    smart_unsub_count = min(len(unfollow_candidates), plan["unsubscribes"])

    keyboard = ReplyKeyboardMarkup([["✅ СТАРТ"], ["❌ Отмена"]], resize_keyboard=True)
    await update.message.reply_text(
        f"📅 *ПЛАН НА СЕГОДНЯ* (День {day_number}/7)\n\n"
        f"🎯 Целевой аккаунт: @{target}\n"
        f"➕ Подписок: {plan['subscribes']} (сегодня уже: {sub_done}/{sub_limit})\n"
        f"➖ Отписок: {smart_unsub_count} (не ответили за {UNFOLLOW_AFTER_DAYS}+ дн.)\n"
        f"❤️ Лайков: {plan['likes']}\n\n"
        f"⏱ Пауза: {MIN_PAUSE}–{MAX_PAUSE} сек\n"
        f"🕐 Рабочие часы: {WORK_HOUR_START}:00–{WORK_HOUR_END}:00\n\n"
        "Нажми *СТАРТ* для запуска:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return WAITING_CONFIRM


# ══════════════════════════════════════════════
# ВЫПОЛНЕНИЕ ПЛАНА
# ══════════════════════════════════════════════

async def execute_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "Отмена" in text:
        return await cmd_cancel(update, context)
    if "СТАРТ" not in text.upper():
        await update.message.reply_text("Нажми '✅ СТАРТ' или '❌ Отмена'.")
        return WAITING_CONFIRM

    acc_id    = context.user_data["active_account_id"]
    ig_login  = context.user_data["active_login"]
    target    = context.user_data["target_account"]
    plan      = context.user_data["plan"]
    plan_id   = context.user_data["plan_id"]

    await update.message.reply_text(
        "🚀 Запускаю план...",
        reply_markup=ReplyKeyboardRemove(),
    )

    # Загружаем клиент с сессией
    with get_conn() as c:
        acc = c.execute("SELECT * FROM ig_accounts WHERE id=?", (acc_id,)).fetchone()
    client = make_client(acc["session_file"])

    # ── Ждём рабочего времени ────────────────────────────
    await wait_until_work_time(update)

    # ── ПОДПИСКИ ─────────────────────────────────────────
    sub_count = plan.get("subscribes", 0)
    subs_done = 0
    if sub_count > 0:
        await update.message.reply_text(
            f"➕ Ищу живых подписчиков @{target} (фильтрую ботов)..."
        )
        followers = client.get_filtered_followers(target, amount=sub_count + 20)

        for ig_uid, ig_uname in followers:
            if subs_done >= sub_count:
                break

            # Ночное окно — пауза
            if not is_work_time():
                await wait_until_work_time(update)

            # Дневной лимит
            done_today, limit = check_daily_limit(acc_id, "subscribe")
            if done_today >= limit:
                await update.message.reply_text(f"🚫 Дневной лимит подписок ({limit}) достигнут!")
                break

            # Чёрный список
            if is_blacklisted(acc_id, ig_uid):
                continue

            ok = client.follow(ig_uid)
            status = "ok" if ok else "err"
            log_action(acc_id, "subscribe", ig_uid, ig_uname, status)
            if ok:
                add_to_blacklist(acc_id, ig_uid, "subscribed")
                subs_done += 1
                with get_conn() as c:
                    c.execute(
                        "UPDATE daily_plans SET subscribes_done=? WHERE id=?",
                        (subs_done, plan_id),
                    )

            await asyncio.sleep(random.randint(MIN_PAUSE, MAX_PAUSE))

        await update.message.reply_text(f"✅ Подписки: {subs_done}/{sub_count}")

    # ── ЛАЙКИ ────────────────────────────────────────────
    like_count = plan.get("likes", 0)
    likes_done = 0
    if like_count > 0:
        await update.message.reply_text(f"❤️ Ставлю лайки ({like_count})...")
        followers = client.get_filtered_followers(target, amount=like_count + 10)

        for ig_uid, ig_uname in followers:
            if likes_done >= like_count:
                break

            if not is_work_time():
                await wait_until_work_time(update)

            done_today, limit = check_daily_limit(acc_id, "like")
            if done_today >= limit:
                await update.message.reply_text(f"🚫 Дневной лимит лайков ({limit}) достигнут!")
                break

            ok = client.like_last_post(ig_uid)
            log_action(acc_id, "like", ig_uid, ig_uname, "ok" if ok else "err")
            if ok:
                likes_done += 1
                with get_conn() as c:
                    c.execute(
                        "UPDATE daily_plans SET likes_done=? WHERE id=?",
                        (likes_done, plan_id),
                    )

            await asyncio.sleep(random.randint(MIN_PAUSE, MAX_PAUSE))

        await update.message.reply_text(f"✅ Лайки: {likes_done}/{like_count}")

    # ── УМНЫЕ ОТПИСКИ ────────────────────────────────────
    unsub_count = plan.get("unsubscribes", 0)
    unsubs_done = 0
    if unsub_count > 0:
        await update.message.reply_text(
            f"➖ Анализирую кто не подписался в ответ (>{UNFOLLOW_AFTER_DAYS} дн.)..."
        )

        candidates = get_unresponded_follows(acc_id)
        # Проверяем через API кто реально не подписан на нас
        not_following_back = client.get_my_followings_not_following_back(ig_login, candidates)
        to_unfollow = not_following_back[:unsub_count]

        await update.message.reply_text(
            f"➖ Найдено {len(to_unfollow)} аккаунтов для отписки..."
        )

        for ig_uid in to_unfollow:
            if not is_work_time():
                await wait_until_work_time(update)

            done_today, limit = check_daily_limit(acc_id, "unsubscribe")
            if done_today >= limit:
                await update.message.reply_text(f"🚫 Дневной лимит отписок ({limit}) достигнут!")
                break

            ok = client.unfollow(ig_uid)
            log_action(acc_id, "unsubscribe", ig_uid, "", "ok" if ok else "err")
            if ok:
                add_to_blacklist(acc_id, ig_uid, "unsubscribed")
                unsubs_done += 1
                with get_conn() as c:
                    c.execute(
                        "UPDATE daily_plans SET unsubscribes_done=? WHERE id=?",
                        (unsubs_done, plan_id),
                    )

            await asyncio.sleep(random.randint(MIN_PAUSE, MAX_PAUSE))

        await update.message.reply_text(f"✅ Отписки: {unsubs_done}/{unsub_count}")

    # Итог
    await update.message.reply_text(
        "🎉 *Готово!*\n\n"
        f"➕ Подписок: {subs_done}/{sub_count}\n"
        f"➖ Отписок: {unsubs_done}/{unsub_count}\n"
        f"❤️ Лайков: {likes_done}/{like_count}\n\n"
        "📊 /status — полная статистика",
        parse_mode="Markdown",
        reply_markup=main_keyboard(has_accounts=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════
# СТАТИСТИКА
# ══════════════════════════════════════════════

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    active = get_active_account(user_id)
    if not active:
        await update.message.reply_text("Нет аккаунтов. Добавь через /start")
        return

    acc_id = active["id"]

    with get_conn() as c:
        stats = c.execute(
            """SELECT action_type, status, COUNT(*) as cnt
               FROM action_log WHERE account_id=?
               GROUP BY action_type, status""",
            (acc_id,),
        ).fetchall()

        today_stats = c.execute(
            """SELECT action_type, COUNT(*) as cnt
               FROM action_log
               WHERE account_id=? AND date(created_at)=date('now')
               GROUP BY action_type""",
            (acc_id,),
        ).fetchall()

        bl_count = c.execute(
            "SELECT COUNT(*) as cnt FROM blacklist WHERE account_id=?", (acc_id,)
        ).fetchone()["cnt"]

        total_days = c.execute(
            "SELECT COUNT(*) as cnt FROM daily_plans WHERE account_id=?", (acc_id,)
        ).fetchone()["cnt"]

        last_plan = c.execute(
            "SELECT * FROM daily_plans WHERE account_id=? ORDER BY id DESC LIMIT 1",
            (acc_id,),
        ).fetchone()

    # Сводка за всё время
    all_time = {}
    for row in stats:
        k = row["action_type"]
        if k not in all_time:
            all_time[k] = 0
        if row["status"] == "ok":
            all_time[k] += row["cnt"]

    today_map = {r["action_type"]: r["cnt"] for r in today_stats}

    icons = {"subscribe": "➕", "unsubscribe": "➖", "like": "❤️"}

    all_time_text = ""
    for act, cnt in all_time.items():
        all_time_text += f"  {icons.get(act,'•')} {act}: {cnt}\n"

    today_text = ""
    for act, lim in DAILY_LIMITS.items():
        done = today_map.get(act, 0)
        today_text += f"  {icons.get(act,'•')} {act}: {done}/{lim}\n"

    plan_text = "—"
    if last_plan:
        plan_text = (
            f"День {last_plan['day_number']}/7 | @{last_plan['target_account']}\n"
            f"  ➕ {last_plan['subscribes_done']}/{last_plan['subscribes_plan']}\n"
            f"  ➖ {last_plan['unsubscribes_done']}/{last_plan['unsubscribes_plan']}\n"
            f"  ❤️ {last_plan['likes_done']}/{last_plan['likes_plan']}"
        )

    accounts = get_all_accounts(user_id)

    await update.message.reply_text(
        f"📊 *СТАТИСТИКА*\n\n"
        f"🟢 Аккаунт: @{active['ig_login']}\n"
        f"📋 Всего аккаунтов: {len(accounts)}\n"
        f"📅 Дней работы: {total_days}\n"
        f"🚫 В чёрном списке: {bl_count}\n\n"
        f"*Сегодня (лимит {DAILY_LIMITS['subscribe']}/день):*\n{today_text}\n"
        f"*Последний план:*\n{plan_text}\n\n"
        f"*За всё время:*\n{all_time_text or '  Пока нет данных'}",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════
# ОТМЕНА И ПОМОЩЬ
# ══════════════════════════════════════════════

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Отменено.",
        reply_markup=main_keyboard(has_accounts=True),
    )
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды:*\n\n"
        "/start — главное меню\n"
        "/switch — сменить аккаунт\n"
        "/status — статистика\n"
        "/cancel — отменить действие\n"
        "/help — эта справка\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*Кнопки:*\n"
        "▶️ Начать день — выбор цели и запуск\n"
        "➕ Добавить аккаунт — добавить новый Instagram\n"
        "🔄 Сменить аккаунт — переключить активный\n"
        "📊 Статистика — посмотреть прогресс\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*Циклы:*\n"
        "Дни 1–6: 25 подписок / 10 отписок / 15 лайков\n"
        "День 7: 10 подписок / 30 отписок (чистка)\n\n"
        f"*Лимиты/день:* подписок {DAILY_LIMITS['subscribe']}, "
        f"отписок {DAILY_LIMITS['unsubscribe']}, "
        f"лайков {DAILY_LIMITS['like']}\n"
        f"*Рабочие часы:* {WORK_HOUR_START}:00 – {WORK_HOUR_END}:00",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════
# ОБРАБОТКА КНОПОК ГЛАВНОГО МЕНЮ (текстовые)
# ══════════════════════════════════════════════

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "▶️ Начать день":
        return await start_day(update, context)
    elif text == "➕ Добавить аккаунт":
        return await add_account_start(update, context)
    elif text == "🔄 Сменить аккаунт":
        return await cmd_switch(update, context)
    elif text == "📊 Статистика":
        await cmd_status(update, context)
        return ConversationHandler.END
    elif text == "❓ Помощь":
        await cmd_help(update, context)
        return ConversationHandler.END
    return ConversationHandler.END


# ══════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════

def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    # Главный ConversationHandler
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.Regex(r"^(▶️ Начать день|➕ Добавить аккаунт|🔄 Сменить аккаунт|📊 Статистика|❓ Помощь)$"), menu_handler),
        ],
        states={
            ADD_LOGIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_login)
            ],
            ADD_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_password)
            ],
            WAITING_TARGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_target)
            ],
            WAITING_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, execute_plan)
            ],
            SWITCH_ACCOUNT: [
                CallbackQueryHandler(switch_callback, pattern=r"^switch_")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("🤖 Бот запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
