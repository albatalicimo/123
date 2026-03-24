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

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Конфигурация
STEAM_API_KEY = "56716E5D4FE456305205C86778E0824E"
TELEGRAM_BOT_TOKEN = "7643881318:AAF-vT733q8-LJEa59guE9U7fE3vpBaU2mM"
CHECK_INTERVAL = 60  # Интервал проверки в секундах

# Глобальные переменные
user_tracking = {}
tasks = {}  # Для хранения фоновых задач
status_history = {}  # Для хранения истории статусов

class StatusPeriod:
    def __init__(self, status: int, start_time: datetime, end_time: datetime = None, game_info: dict = None):
        self.status = status
        self.start_time = start_time
        self.end_time = end_time or datetime.now()
        self.game_info = game_info or {}
    
    @property
    def duration(self) -> timedelta:
        return self.end_time - self.start_time
    
    def __repr__(self):
        game_str = f" 🎮 {self.game_info.get('name', '')}" if self.game_info else ""
        status_name = "🟠 Отошел во время игры" if self.status == 3 and self.game_info else get_status_name(self.status)
        return (f"{status_name}{game_str}\n"
                f"⏱️ В статусе: {format_time_delta(self.duration)} "
                f"в период с {self.start_time.strftime('%H:%M')} по {self.end_time.strftime('%H:%M')}")

async def get_steam_user_summary(steam_id: str) -> dict:
    url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                return data['response']['players'][0] if data.get('response', {}).get('players') else None
    except Exception as e:
        logging.error(f"Ошибка при запросе к Steam API: {e}")
        return None

async def get_steam_game_info(appid: int) -> dict:
    if not appid:
        return None
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                if str(appid) in data and data[str(appid)]['success']:
                    return {
                        'name': data[str(appid)]['data']['name'],
                        'appid': appid
                    }
                return None
    except Exception as e:
        logging.error(f"Ошибка при запросе информации об игре: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keyboard = [
            [InlineKeyboardButton("📋 Отслеживаемые пользователи", callback_data='list_users')],
            [InlineKeyboardButton("➕ Добавить отслеживание", callback_data='add_tracking')],
            [InlineKeyboardButton("➖ Удалить отслеживание", callback_data='remove_tracking')],
            [InlineKeyboardButton("📊 Получить отчет", callback_data='get_report')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.message:
            await update.message.reply_text(
                "🚀 Бот для отслеживания статуса Steam аккаунтов\n\n"
                "Выберите действие:",
                reply_markup=reply_markup
            )
        else:
            await update.callback_query.edit_message_text(
                "🚀 Бот для отслеживания статуса Steam аккаунтов\n\n"
                "Выберите действие:",
                reply_markup=reply_markup
            )
    except Exception as e:
        logging.error(f"Ошибка в start: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'list_users':
        await list_tracking(update, context)
    elif query.data == 'add_tracking':
        await query.edit_message_text("Введите SteamID пользователя для отслеживания (17 цифр):")
        context.user_data['awaiting_steamid'] = True
    elif query.data == 'remove_tracking':
        await show_remove_tracking_menu(update, context)
    elif query.data == 'get_report':
        await show_report_menu(update, context)
    elif query.data.startswith('report_'):
        steam_id = query.data[7:]
        await generate_user_report(update, context, steam_id)
    elif query.data.startswith('remove_'):
        steam_id = query.data[7:]
        await remove_tracking(update, context, steam_id)
    elif query.data == 'back_to_menu':
        await start(update, context)

# Остальные функции остаются без изменений (show_remove_tracking_menu, remove_tracking, show_report_menu, generate_user_report, handle_message, list_tracking, get_status_name, format_time_delta, check_user_status, stop_tracking)

async def show_remove_tracking_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in user_tracking or not user_tracking[chat_id]:
        await query.edit_message_text("ℹ️ Нет отслеживаемых аккаунтов")
        return
    
    keyboard = []
    for steam_id, data in user_tracking[chat_id].items():
        keyboard.append([InlineKeyboardButton(
            f"{data['name']} ({steam_id})", 
            callback_data=f'remove_{steam_id}'
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("Выберите пользователя для удаления:", reply_markup=reply_markup)

async def remove_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE, steam_id: str):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in user_tracking or steam_id not in user_tracking[chat_id]:
        await query.answer("ℹ️ Этот пользователь не отслеживается")
        return
    
    user_name = user_tracking[chat_id][steam_id]['name']
    
    task = tasks.get((chat_id, steam_id))
    if task:
        task.cancel()
        del tasks[(chat_id, steam_id)]
    
    del user_tracking[chat_id][steam_id]
    if chat_id in status_history and steam_id in status_history[chat_id]:
        del status_history[chat_id][steam_id]
    
    if not user_tracking[chat_id]:
        del user_tracking[chat_id]
        if chat_id in status_history:
            del status_history[chat_id]
    
    keyboard = [
        [InlineKeyboardButton("📋 Отслеживаемые пользователи", callback_data='list_users')],
        [InlineKeyboardButton("🏠 В главное меню", callback_data='back_to_menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⏹ Прекратил отслеживание:\nИмя: {user_name}\nSteamID: {steam_id}",
        reply_markup=reply_markup
    )

async def show_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in user_tracking or not user_tracking[chat_id]:
        await query.edit_message_text("ℹ️ Нет отслеживаемых аккаунтов")
        return
    
    keyboard = []
    for steam_id, data in user_tracking[chat_id].items():
        keyboard.append([InlineKeyboardButton(
            f"{data['name']} ({steam_id})", 
            callback_data=f'report_{steam_id}'
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("Выберите пользователя для отчета:", reply_markup=reply_markup)

async def generate_user_report(update: Update, context: ContextTypes.DEFAULT_TYPE, steam_id: str):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in status_history or steam_id not in status_history[chat_id]:
        await query.answer("Нет данных для отчета")
        return
    
    user_data = user_tracking[chat_id][steam_id]
    history = status_history[chat_id][steam_id]
    
    now = datetime.now()
    current_period = history['current_period']
    current_period.end_time = now
    all_periods = history['status_periods'] + [current_period]
    
    report_lines = [
        f"👤 {user_data['name']}",
        f"🆔 {steam_id}",
        f"\n📊 История активности:"
    ]
    
    for period in sorted(all_periods, key=lambda x: x.start_time):
        if period.duration.total_seconds() < 60:
            continue
        report_lines.append(str(period))
    
    if len(report_lines) == 3:
        report_lines.append("Нет данных о смене статусов")
    
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data='get_report')],
        [InlineKeyboardButton("🏠 В главное меню", callback_data='back_to_menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("\n\n".join(report_lines), reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_steamid' in context.user_data:
        steam_id = update.message.text.strip()
        context.user_data['awaiting_steamid'] = False
        
        if not steam_id.isdigit() or len(steam_id) != 17:
            await update.message.reply_text("❌ Неверный формат SteamID\nДолжен состоять из 17 цифр")
            return
            
        user_info = await get_steam_user_summary(steam_id)
        
        if not user_info:
            await update.message.reply_text("⚠️ Не удалось получить данные по SteamID")
            return
        
        user_name = user_info.get('personaname', 'Неизвестный пользователь')
        current_status = user_info.get('personastate', 0)
        game_info = None
        if 'gameextrainfo' in user_info:
            game_info = {'name': user_info['gameextrainfo'], 'appid': user_info.get('gameid')}
        
        chat_id = update.message.chat_id
        if chat_id not in user_tracking:
            user_tracking[chat_id] = {}
            status_history[chat_id] = {}
        
        if steam_id in user_tracking[chat_id]:
            await update.message.reply_text(f"ℹ️ Уже отслеживаю {user_name}")
            return
        
        now = datetime.now()
        user_tracking[chat_id][steam_id] = {
            'last_status': current_status,
            'last_game': game_info,
            'last_check': now,
            'status_start_time': now,
            'name': user_name
        }
        
        status_history[chat_id][steam_id] = {
            'current_period': StatusPeriod(current_status, now, game_info=game_info),
            'status_periods': []
        }
        
        task = asyncio.create_task(check_user_status(chat_id, steam_id, context.application))
        tasks[(chat_id, steam_id)] = task
        
        await update.message.reply_text(
            f"✅ Начал отслеживание:\n"
            f"👤 {user_name}\n"
            f"🆔 {steam_id}\n"
            f"Статус: {get_status_name(current_status)}"
            f"{f' 🎮 {game_info['name']}' if game_info else ''}"
        )

async def list_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if chat_id not in user_tracking or not user_tracking[chat_id]:
        await query.edit_message_text("ℹ️ Нет отслеживаемых аккаунтов")
        return
    
    message = "📋 Отслеживаю:\n\n"
    for steam_id, data in user_tracking[chat_id].items():
        time_in_status = format_time_delta(datetime.now() - data['status_start_time'])
        message += f"👤 {data['name']}\n🆔 {steam_id}\n📊 {get_status_name(data['last_status'])}{f' 🎮 {data['last_game']['name']}' if data['last_game'] else ''}\n⏱ В статусе: {time_in_status}\n──────────────────\n"
    
    keyboard = [
        [InlineKeyboardButton("📊 Получить отчет", callback_data='get_report')],
        [InlineKeyboardButton("➖ Удалить отслеживание", callback_data='remove_tracking')],
        [InlineKeyboardButton("➕ Добавить отслеживание", callback_data='add_tracking')],
        [InlineKeyboardButton("🏠 В главное меню", callback_data='back_to_menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup)

def get_status_name(status_code: int) -> str:
    status_map = {
        0: "🔴 Оффлайн", 1: "🟢 Онлайн", 2: "🟡 Занят",
        3: "🟠 Отошёл", 4: "💤 Спит", 5: "💰 Хочет торговать", 6: "🎮 Хочет играть"
    }
    return status_map.get(status_code, "❓ Неизвестно")

def format_time_delta(delta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"

async def check_user_status(chat_id: int, steam_id: str, app: Application) -> None:
    while True:
        try:
            if chat_id not in user_tracking or steam_id not in user_tracking[chat_id]:
                break
                
            user_data = user_tracking[chat_id][steam_id]
            user_info = await get_steam_user_summary(steam_id)
            
            if user_info:
                current_status = user_info.get('personastate', 0)
                current_game = None
                if 'gameextrainfo' in user_info:
                    current_game = {'name': user_info['gameextrainfo'], 'appid': user_info.get('gameid')}
                
                last_status = user_data['last_status']
                last_game = user_data['last_game']
                
                status_changed = current_status != last_status
                game_changed = (current_game != last_game) or (current_game and last_game and current_game.get('appid') != last_game.get('appid'))
                
                if status_changed or game_changed:
                    time_in_status = format_time_delta(datetime.now() - user_data['status_start_time'])
                    
                    message_lines = [f"🔄 Изменение статуса {user_data['name']}:"]
                    
                    if status_changed:
                        message_lines.append(f"Был: {get_status_name(last_status)}{f' 🎮 {last_game['name']}' if last_game else ''}")
                        message_lines.append(f"В статусе: {time_in_status}")
                        message_lines.append(f"Стал: {get_status_name(current_status)}{f' 🎮 {current_game['name']}' if current_game else ''}")
                    elif game_changed:
                        if current_game and not last_game:
                            message_lines.append(f"🔼 Начал играть в: {current_game['name']}")
                        elif not current_game and last_game:
                            message_lines.append(f"🔽 Перестал играть в: {last_game['name']}")
                    
                    await app.bot.send_message(chat_id=chat_id, text="\n".join(message_lines))
                    
                    # Обновляем историю
                    now = datetime.now()
                    history = status_history[chat_id][steam_id]
                    history['current_period'].end_time = now
                    history['status_periods'].append(history['current_period'])
                    history['current_period'] = StatusPeriod(current_status, now, game_info=current_game)
                    
                    user_data['last_status'] = current_status
                    user_data['last_game'] = current_game
                    user_data['status_start_time'] = now
            
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logging.error(f"Ошибка в check_user_status: {e}")
            await asyncio.sleep(10)

async def stop_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        if not context.args:
            await update.message.reply_text("ℹ️ Укажите SteamID после /stop")
            return
        steam_id = context.args[0]
        
        if chat_id in user_tracking and steam_id in user_tracking[chat_id]:
            user_name = user_tracking[chat_id][steam_id]['name']
            
            task = tasks.get((chat_id, steam_id))
            if task:
                task.cancel()
                del tasks[(chat_id, steam_id)]
            
            del user_tracking[chat_id][steam_id]
            if chat_id in status_history and steam_id in status_history[chat_id]:
                del status_history[chat_id][steam_id]
            
            if not user_tracking.get(chat_id):
                user_tracking.pop(chat_id, None)
                status_history.pop(chat_id, None)
            
            await update.message.reply_text(f"⏹ Прекратил отслеживание {user_name}")
        else:
            await update.message.reply_text("ℹ️ Не отслеживаю этого пользователя")
    except Exception as e:
        logging.error(f"Ошибка в stop_tracking: {e}")

def main() -> None:
    """Запуск бота на bothost через webhook"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop_tracking))
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
