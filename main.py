import telebot
from telebot import types
import requests
import threading
import time
import logging
from datetime import datetime, timedelta
from cachetools import cached, TTLCache
from babel import Locale, negotiate_locale
from babel.support import Translations

# Замените 'YOUR_BOT_TOKEN' на токен вашего бота
BOT_TOKEN = '_________________________________'
API_KEY = '_________________________________________'  # Замените на ваш API ключ для Wildberries API
ADMIN_CHAT_ID = '____________________________'  # Замените на ваш ID администратора

bot = telebot.TeleBot(BOT_TOKEN)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Кэш для складов (кэшируется на 1 час)
warehouse_cache = TTLCache(maxsize=100, ttl=3600)

# Хранилище данных пользователей
user_data = {}
active_search_threads = {}  # Хранилище для активных потоков поиска

# Доступные типы упаковки
PACKAGE_TYPES = ['Короба', 'Монопаллеты', 'Суперсейф', 'QR-поставка с коробами']

# Поддерживаемые языки
LANGUAGES = ['ru', 'en']
DEFAULT_LANGUAGE = 'ru'


# Функция для загрузки переводов
def load_translations(locale):
    translations = Translations.load('locale', locales=locale)
    return translations


# Функция для определения языка пользователя
def get_user_language(chat_id):
    return user_data.get(chat_id, {}).get('language', DEFAULT_LANGUAGE)


# Функция для перевода текста
def translate(chat_id, text_id):
    language = get_user_language(chat_id)
    locale = Locale.parse(language)
    translations = load_translations(locale)
    return translations.gettext(text_id)


# Функция для получения списка складов с кэшированием
@cached(warehouse_cache)
def get_warehouses():
    try:
        url = 'https://supplies-api.wildberries.ru/api/v1/warehouses'
        headers = {
            'Authorization': API_KEY
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        logger.info(f"Список складов успешно получен.")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе складов: {e}")
        return []


# Функция для получения коэффициентов приёмки
def get_acceptance_coefficients(warehouse_id):
    try:
        url = f'https://supplies-api.wildberries.ru/api/v1/acceptance/coefficients?warehouseIDs={warehouse_id}'
        headers = {
            'Authorization': API_KEY
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        logger.info(f"Коэффициенты приёмки для склада {warehouse_id} успешно получены.")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе коэффициентов для склада {warehouse_id}: {e}")
        return []


# Стартовое сообщение
@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    language = negotiate_locale([message.from_user.language_code], LANGUAGES) or DEFAULT_LANGUAGE
    user_data[chat_id] = {'language': language}
    logger.info(f"Новый пользователь {chat_id} начал работу с ботом.")
    bot.send_message(chat_id, translate(chat_id,
                                        "Привет! Я помогу тебе найти подходящие условия для поставок.\nДавай начнем. В каком городе ты ищешь склад?"))


# Получение города от пользователя
@bot.message_handler(func=lambda message: message.chat.id in user_data and 'city' not in user_data[message.chat.id])
def get_city(message):
    city = message.text.strip()
    warehouses = get_warehouses()
    matched_warehouses = [w for w in warehouses if
                          city.lower() in w['address'].lower() or city.lower() in w['name'].lower()]

    if matched_warehouses:
        user_data[message.chat.id]['city'] = city
        user_data[message.chat.id]['warehouses'] = matched_warehouses
        logger.info(f"Пользователь {message.chat.id} выбрал город: {city}. Найдено {len(matched_warehouses)} складов.")

        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        for w in matched_warehouses:
            markup.add(f"{w['name']} (ID: {w['ID']})")
        bot.send_message(message.chat.id, translate(message.chat.id, "Пожалуйста, выбери склад из списка:"),
                         reply_markup=markup)
    else:
        logger.warning(f"Для пользователя {message.chat.id} в городе {city} не найдено складов.")
        bot.send_message(message.chat.id, translate(message.chat.id,
                                                    "К сожалению, складов в этом городе не найдено. Попробуй ввести другой город."))


# Получение склада от пользователя
@bot.message_handler(
    func=lambda message: message.chat.id in user_data and 'warehouse' not in user_data[message.chat.id] and 'city' in
                         user_data[message.chat.id])
def get_warehouse(message):
    selected = message.text.strip()
    warehouses = user_data[message.chat.id]['warehouses']
    for w in warehouses:
        warehouse_info = f"{w['name']} (ID: {w['ID']})"
        if selected == warehouse_info:
            user_data[message.chat.id]['warehouse'] = w
            logger.info(f"Пользователь {message.chat.id} выбрал склад: {warehouse_info}.")

            markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
            for p_type in PACKAGE_TYPES:
                markup.add(p_type)
            bot.send_message(message.chat.id, translate(message.chat.id, "Выбери тип упаковки:"), reply_markup=markup)
            return
    logger.warning(f"Пользователь {message.chat.id} выбрал неизвестный склад: {selected}.")
    bot.send_message(message.chat.id, translate(message.chat.id, "Пожалуйста, выбери склад из предложенного списка."))


# Получение типа упаковки от пользователя
@bot.message_handler(func=lambda message: message.chat.id in user_data and 'package_type' not in user_data[
    message.chat.id] and 'warehouse' in user_data[message.chat.id])
def get_package_type(message):
    package_type = message.text.strip()
    if package_type in PACKAGE_TYPES:
        user_data[message.chat.id]['package_type'] = package_type
        logger.info(f"Пользователь {message.chat.id} выбрал тип упаковки: {package_type}.")
        bot.send_message(message.chat.id,
                         translate(message.chat.id, "Укажи требуемый коэффициент (например, 1, 0, -1):"))
    else:
        logger.warning(f"Пользователь {message.chat.id} выбрал неизвестный тип упаковки: {package_type}.")
        bot.send_message(message.chat.id,
                         translate(message.chat.id, "Пожалуйста, выбери тип упаковки из предложенного списка."))


# Получение коэффициента от пользователя
@bot.message_handler(func=lambda message: message.chat.id in user_data and 'coefficient' not in user_data[
    message.chat.id] and 'package_type' in user_data[message.chat.id])
def get_coefficient(message):
    try:
        coefficient = float(message.text.strip())
        user_data[message.chat.id]['coefficient'] = coefficient
        logger.info(f"Пользователь {message.chat.id} ввел коэффициент: {coefficient}.")
        bot.send_message(message.chat.id, translate(message.chat.id, "Укажи начальную дату в формате ГГГГ-ММ-ДД:"))
    except ValueError:
        logger.warning(f"Пользователь {message.chat.id} ввел некорректный коэффициент: {message.text.strip()}.")
        bot.send_message(message.chat.id,
                         translate(message.chat.id, "Пожалуйста, введи числовое значение для коэффициента."))


# Получение начальной даты от пользователя
@bot.message_handler(func=lambda message: message.chat.id in user_data and 'start_date' not in user_data[
    message.chat.id] and 'coefficient' in user_data[message.chat.id])
def get_start_date(message):
    try:
        start_date = datetime.strptime(message.text.strip(), '%Y-%m-%d')
        user_data[message.chat.id]['start_date'] = start_date
        logger.info(f"Пользователь {message.chat.id} ввел начальную дату: {start_date}.")
        bot.send_message(message.chat.id, translate(message.chat.id, "Укажи конечную дату в формате ГГГГ-ММ-ДД:"))
    except ValueError:
        logger.warning(f"Пользователь {message.chat.id} ввел некорректную дату: {message.text.strip()}.")
        bot.send_message(message.chat.id,
                         translate(message.chat.id, "Пожалуйста, введи дату в корректном формате ГГГГ-ММ-ДД."))


# Получение конечной даты от пользователя
@bot.message_handler(func=lambda message: message.chat.id in user_data and 'end_date' not in user_data[
    message.chat.id] and 'start_date' in user_data[message.chat.id])
def get_end_date(message):
    try:
        end_date = datetime.strptime(message.text.strip(), '%Y-%m-%d')
        if end_date >= user_data[message.chat.id]['start_date']:
            user_data[message.chat.id]['end_date'] = end_date
            logger.info(f"Пользователь {message.chat.id} ввел конечную дату: {end_date}. Начат поиск условий.")
            bot.send_message(message.chat.id, translate(message.chat.id,
                                                        "Отлично! Я начну поиск подходящих условий и сообщу тебе, как только что-то найду."))
            # Запуск отдельного потока для периодической проверки
            search_thread = threading.Thread(target=check_conditions, args=(message.chat.id,), daemon=True)
            search_thread.start()
            active_search_threads[message.chat.id] = search_thread
        else:
            logger.warning(f"Пользователь {message.chat.id} ввел конечную дату раньше начальной: {end_date}.")
            bot.send_message(message.chat.id, translate(message.chat.id,
                                                        "Конечная дата не может быть раньше начальной. Пожалуйста, введи корректную дату."))
    except ValueError:
        logger.warning(f"Пользователь {message.chat.id} ввел некорректную конечную дату: {message.text.strip()}.")
        bot.send_message(message.chat.id,
                         translate(message.chat.id, "Пожалуйста, введи дату в корректном формате ГГГГ-ММ-ДД."))


# Функция для периодической проверки условий
def check_conditions(chat_id):
    user_info = user_data[chat_id]
    warehouse_id = user_info['warehouse']['ID']
    target_package_type = user_info['package_type']
    target_coefficient = user_info['coefficient']
    start_date = user_info['start_date']
    end_date = user_info['end_date']

    logger.info(f"Начата проверка условий для пользователя {chat_id}.")
    while True:
        try:
            coefficients = get_acceptance_coefficients(warehouse_id)
            for item in coefficients:
                item_date = datetime.strptime(item['date'], '%Y-%m-%dT%H:%M:%SZ')
                if start_date <= item_date <= end_date:
                    if item['boxTypeName'] == target_package_type and item['coefficient'] == target_coefficient:
                        message = (
                            f"{translate(chat_id, 'Найдены подходящие условия!')}\n\n"
                            f"📅 {translate(chat_id, 'Дата')}: {item_date.strftime('%Y-%m-%d')}\n"
                            f"🏬 {translate(chat_id, 'Склад')}: {user_info['warehouse']['name']}\n"
                            f"📦 {translate(chat_id, 'Тип упаковки')}: {item['boxTypeName']}\n"
                            f"⚖️ {translate(chat_id, 'Коэффициент')}: {item['coefficient']}"
                        )
                        bot.send_message(chat_id, message)
                        logger.info(f"Условия найдены и отправлены пользователю {chat_id}.")
                        # Удаляем данные пользователя после успешного поиска
                        del user_data[chat_id]
                        del active_search_threads[chat_id]
                        return
            time.sleep(60)  # Ожидание перед следующей проверкой (например, 1 минута)
        except Exception as e:
            logger.error(f"Ошибка при проверке условий для пользователя {chat_id}: {e}")
            bot.send_message(chat_id, translate(chat_id, "Произошла ошибка при проверке условий. Попробуй позже."))
            del active_search_threads[chat_id]
            return


# Административные команды для управления ботом
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    chat_id = message.chat.id
    if str(chat_id) == ADMIN_CHAT_ID:  # Замените на ваш ID администратора
        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        markup.add("Активные запросы", "Очистить кэш", "Статистика")
        bot.send_message(chat_id, "Выберите действие:", reply_markup=markup)
    else:
        bot.send_message(chat_id, translate(chat_id, "У вас нет прав для использования этой команды."))


@bot.message_handler(func=lambda message: message.text == "Активные запросы")
def show_active_requests(message):
    chat_id = message.chat.id
    if str(chat_id) == ADMIN_CHAT_ID:
        if user_data:
            active_requests = "\n".join(
                [f"{uid}: {info['city']}, склад {info['warehouse']['name']}" for uid, info in user_data.items()])
            bot.send_message(chat_id, f"Активные запросы:\n{active_requests}")
        else:
            bot.send_message(chat_id, "Нет активных запросов.")
    else:
        bot.send_message(chat_id, translate(chat_id, "У вас нет прав для использования этой команды."))


@bot.message_handler(func=lambda message: message.text == "Очистить кэш")
def clear_cache(message):
    chat_id = message.chat.id
    if str(chat_id) == ADMIN_CHAT_ID:
        warehouse_cache.clear()
        bot.send_message(chat_id, "Кэш складов очищен.")
        logger.info("Кэш складов очищен администратором.")
    else:
        bot.send_message(chat_id, translate(chat_id, "У вас нет прав для использования этой команды."))


@bot.message_handler(func=lambda message: message.text == "Статистика")
def show_statistics(message):
    chat_id = message.chat.id
    if str(chat_id) == ADMIN_CHAT_ID:
        bot.send_message(chat_id, f"Всего пользователей: {len(user_data)}\nСкладов в кэше: {len(warehouse_cache)}")
    else:
        bot.send_message(chat_id, translate(chat_id, "У вас нет прав для использования этой команды."))


# Обработка неизвестных сообщений
@bot.message_handler(func=lambda message: True)
def unknown_message(message):
    bot.send_message(message.chat.id,
                     translate(message.chat.id, "Извини, я не понимаю это сообщение. Пожалуйста, следуй инструкциям."))
    logger.warning(f"Пользователь {message.chat.id} отправил неизвестное сообщение: {message.text}")


# Запуск бота
if __name__ == '__main__':
    logger.info("Бот запущен и готов к работе.")
    bot.infinity_polling()
