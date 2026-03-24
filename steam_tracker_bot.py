import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler
)

# ====================== НАСТРОЙКИ ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

STEAM_API_KEY = "56716E5D4FE456305205C86778E0824E"
TELEGRAM_BOT_TOKEN = "7643881318:AAF-vT733q8-LJEa59guE9U7fE3vpBaU2mM"
CHECK_INTERVAL = 60

user_tracking = {}
tasks = {}
status_history = {}


class StatusPeriod:
    def __init__(self, status: int, start_time: datetime, end_time=None, game_info=None):
        self.status = status
        self.start_time = start_time
        self.end_time = end_time or datetime.now()
        self.game_info = game_info or {}

    @property
    def duration(self) -> timedelta:
        return self.end_time - self.start_time

    def __repr__(self):
        game_str = f" 🎮 {self.game_info.get('name', '')}" if self.game_info.get('name') else ""
        status_name = "🟠 Отошел во время игры" if self.status == 3 and self.game_info else get_status_name(self.status)
        return f"{status_name}{game_str}\n⏱️ В статусе: {format_time_delta(self.duration)} с {self.start_time.strftime('%H:%M')} по {self.end_time.strftime('%H:%M')}"


async def get_steam_user_summary(steam_id: str):
    url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data['response']['players'][0] if data.get('response', {}).get('players') else None
    except Exception as e:
        logging.error(f"Steam API error: {e}")
        return None


async def get_steam_game_info(appid: int):
    if not appid:
        return None
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if str(appid) in data and data[str(appid)]['success']:
                    return {'name': data[str(appid)]['data']['name'], 'appid': appid}
                return None
    except Exception as e:
        logging.error(f"Game info error: {e}")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📋 Отслеживаемые пользователи", callback_data='list_users')],
        [InlineKeyboardButton("➕ Добавить отслеживание", callback_data='add_tracking')],
        [InlineKeyboardButton("➖ Удалить отслеживание", callback_data='remove_tracking')],
        [InlineKeyboardButton("📊 Получить отчет", callback_data='get_report')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "🚀 Бот для отслеживания статуса Steam аккаунтов\n\nВыберите действие:"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'list_users':
        await list_tracking(update, context)
    elif query.data == 'add_tracking':
        await query.edit_message_text("Введите SteamID (17 цифр):")
        context.user_data['awaiting_steamid'] = True
    elif query.data == 'remove_tracking':
        await show_remove_tracking_menu(update, context)
    elif query.data == 'get_report':
        await show_report_menu(update, context)
    elif query.data.startswith('report_'):
        await generate_user_report(update, context, query.data[7:])
    elif query.data.startswith('remove_'):
        await remove_tracking(update, context, query.data[7:])
    elif query.data == 'back_to_menu':
        await start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_steamid'):
        return

    steam_id = update.message.text.strip()
    context.user_data['awaiting_steamid'] = False

    if not steam_id.isdigit() or len(steam_id) != 17:
        await update.message.reply_text("❌ SteamID должен состоять из 17 цифр")
        return

    user_info = await get_steam_user_summary(steam_id)
    if not user_info:
        await update.message.reply_text("⚠️ Не удалось получить данные по SteamID")
        return

    user_name = user_info.get('personaname', 'Неизвестно')
    current_status = user_info.get('personastate', 0)
    game_info = None
    if user_info.get('gameextrainfo'):
        game_info = {
            'name': user_info['gameextrainfo'],
            'appid': user_info.get('gameid')
        }

    chat_id = update.message.chat_id

    if chat_id not in user_tracking:
        user_tracking[chat_id] = {}
        status_history[chat_id] = {}

    if steam_id in user_tracking[chat_id]:
        await update.message.reply_text(f"⚠️ Уже отслеживаю {user_name}")
        return

    now = datetime.now()

    user_tracking[chat_id][steam_id] = {
        'name': user_name,
        'last_status': current_status,
        'last_game': game_info,
        'status_start_time': now
    }

    status_history[chat_id][steam_id] = {
        'status_periods': [],
        'current_period': StatusPeriod(current_status, now, game_info=game_info)
    }

    task = asyncio.create_task(check_user_status(chat_id, steam_id, context.application))
    tasks[(chat_id, steam_id)] = task

    game_text = f" 🎮 {game_info['name']}" if game_info and game_info.get('name') else ""
    await update.message.reply_text(f"✅ Начал отслеживание\n👤 {user_name}\n🆔 {steam_id}\nСтатус: {get_status_name(current_status)}{game_text}")


async def check_user_status(chat_id: int, steam_id: str, app: Application):
    while True:
        try:
            if chat_id not in user_tracking or steam_id not in user_tracking[chat_id]:
                break

            user_data = user_tracking[chat_id][steam_id]
            user_info = await get_steam_user_summary(steam_id)

            if user_info:
                current_status = user_info.get('personastate', 0)
                current_game = None
                if user_info.get('gameextrainfo'):
                    current_game = {
                        'name': user_info['gameextrainfo'],
                        'appid': user_info.get('gameid')
                    }

                last_status = user_data['last_status']
                last_game = user_data.get('last_game')

                if current_status != last_status or current_game != last_game:
                    time_str = format_time_delta(datetime.now() - user_data['status_start_time'])

                    lines = [f"🔄 Изменение статуса {user_data['name']}:"]
                    
                    if current_status != last_status:
                        old_game = f" 🎮 {last_game['name']}" if last_game and last_game.get('name') else ""
                        new_game = f" 🎮 {current_game['name']}" if current_game and current_game.get('name') else ""
                        lines.append(f"Был: {get_status_name(last_status)}{old_game}")
                        lines.append(f"В статусе: {time_str}")
                        lines.append(f"Стал: {get_status_name(current_status)}{new_game}")
                    else:
                        if current_game and not last_game:
                            lines.append(f"🔼 Начал играть в: {current_game.get('name', '')}")
                        elif not current_game and last_game:
                            lines.append(f"🔽 Перестал играть в: {last_game.get('name', '')}")

                    await app.bot.send_message(chat_id=chat_id, text="\n".join(lines))

                    # Обновление истории
                    now = datetime.now()
                    hist = status_history[chat_id][steam_id]
                    hist['current_period'].end_time = now
                    hist['status_periods'].append(hist['current_period'])
                    hist['current_period'] = StatusPeriod(current_status, now, game_info=current_game)

                    user_data['last_status'] = current_status
                    user_data['last_game'] = current_game
                    user_data['status_start_time'] = now

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.error(f"check_user_status error: {e}")
            await asyncio.sleep(10)


def get_status_name(status: int) -> str:
    names = {
        0: "🔴 Оффлайн", 1: "🟢 Онлайн", 2: "🟡 Занят", 3: "🟠 Отошёл",
        4: "💤 Спит", 5: "💰 Хочет торговать", 6: "🎮 Хочет играть"
    }
    return names.get(status, "❓ Неизвестно")


def format_time_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


# ====================== ЗАПУСК ======================
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Webhook для bothost.ru
    application.run_webhook(
        listen="0.0.0.0",
        port=8080,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"https://bot.bothost.ru/{TELEGRAM_BOT_TOKEN}"
    )


if __name__ == '__main__':
    main()
