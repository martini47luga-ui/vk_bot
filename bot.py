import re
import io
from PIL import Image  # для проверки изображений (необязательно, но полезно)
import os
import logging
import requests
import feedparser
import schedule
import threading
import time
import csv
import datetime
from collections import defaultdict
from dotenv import load_dotenv
import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
import json
import os

# Загружаем переменные окружения из файла .env
load_dotenv()

# ---------- Конфигурация VK ----------
VK_TOKEN = os.getenv("VK_TOKEN")
COMMUNITY_ID = -2192128  # ВАШ ID СООБЩЕСТВА (отрицательное число)

# ---------- Конфигурация OpenRouter ----------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openrouter/free" # быстрая и бесплатная модель

# ---------- Настройка логирования ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Авторизация VK ----------
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

# ---------- Конфигурация репостов ----------
REPOST_SOURCES = [-72378974, -39243732]  # ID групп с минусом (Мой Компьютер и Типичный сисадмин)
REPOST_FILE = "reposted_posts.json"       # файл для хранения ID уже репостнутых постов
REPOST_MESSAGE = "🔧 Полезное из мира IT" # комментарий к репосту (можно изменить)
# -----------------------------------------

# ---------- Хранилище контекста для диалогов ----------
context = {}

# ---------- Для защиты от спама ----------
last_message_time = defaultdict(float)
FLOOD_DELAY = 3  # минимальный интервал между сообщениями (секунд)

# ---------- Множество для отслеживания отправленных предупреждений ----------
disclaimer_sent = set()

# ---------- Файл для логирования диалогов ----------
LOG_FILE = "conversations_log.csv"

def log_conversation(user_id, role, message):
    """Записывает сообщение в CSV-лог."""
    file_exists = os.path.isfile(LOG_FILE)
    try:
        with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "user_id", "role", "message"])
            writer.writerow([datetime.datetime.now().isoformat(), user_id, role, message])
    except Exception as e:
        logger.error(f"Ошибка записи лога: {e}")

def is_flood(user_id):
    """Проверяет, не слишком ли часто пишет пользователь."""
    now = time.time()
    if now - last_message_time[user_id] < FLOOD_DELAY:
        return True
    last_message_time[user_id] = now
    return False

def send_message(user_id, text, keyboard=None):
    """Отправляет сообщение пользователю ВК с опциональной клавиатурой."""
    if not text or not text.strip():
        text = "Извините, не удалось сформировать ответ."
    try:
        vk.messages.send(
            user_id=user_id,
            message=text,
            random_id=0,
            keyboard=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

def get_main_keyboard():
    """Возвращает основную клавиатуру с категориями."""
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('🕹️ Игровые ПК', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('💼 Офисные ПК', color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button('🏠 Домашние/Мультимедиа', color=VkKeyboardColor.SECONDARY)
    keyboard.add_button('📦 Мини-ПК', color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button('📞 Связаться с менеджером', color=VkKeyboardColor.POSITIVE)
    keyboard.add_button('ℹ️ О группе', color=VkKeyboardColor.PRIMARY)
    return keyboard.get_keyboard()

def ask_openrouter_with_context(user_id, user_message):
    """Отправляет запрос в OpenRouter и возвращает ответ с повторными попытками."""
    history = context.get(user_id, [])
    if not history or history[0]['role'] != 'system':
        # Системный промпт с полной информацией о группе, ценах и правилах общения
        system_message = {
            "role": "system",
            "content": (
                "Ты — официальный ассистент группы ВКонтакте «КОМПЬЮТЕРНЫЕ РЕШЕНИЯ - Сборка ПК на заказ». "
                "Группа занимается профессиональной сборкой компьютеров на заказ: игровые ПК, рабочие станции, офисные компьютеры, мультимедийные системы, апгрейд и ремонт.\n\n"

                "**АКТУАЛЬНЫЕ ЦЕНЫ НА КОМПЬЮТЕРЫ (февраль 2026 года):**\n\n"

                "🏢 **Офисные и рабочие ПК:**\n"
                "• Базовый офисный (CRM, документы, 1С, почта) — от 45 000 до 55 000 руб.\n"
                "  *Конфигурация: Intel Core i3-12100 / 16 ГБ DDR4 / 512 ГБ NVMe SSD*\n\n"
                "• Средний офисный (многозадачность, аналитика, удалённая работа) — от 55 000 до 75 000 руб.\n"
                "  *Конфигурация: Intel Core i5-12400 / 16-32 ГБ DDR4 / 512 ГБ NVMe SSD*\n\n"
                "• Премиальный офис / рабочая станция (сложные расчёты, базы данных, виртуализация) — от 75 000 до 110 000 руб.\n"
                "  *Конфигурация: Intel Core i7-13700 / 32 ГБ DDR5 / 1 ТБ NVMe SSD*\n\n"

                "🏠 **Домашние и мультимедийные ПК:**\n"
                "• Базовый домашний (интернет, фильмы, учёба, лёгкие игры) — от 50 000 до 65 000 руб.\n"
                "  *Конфигурация: Ryzen 5 5600G (со встроенной графикой) / 16 ГБ DDR4 / 512 ГБ NVMe SSD*\n\n"
                "• Мультимедийный (просмотр 4K, фото/видео, несложный монтаж) — от 65 000 до 85 000 руб.\n"
                "  *Конфигурация: Intel Core i5-12400 / GT 1030 / 16 ГБ DDR4 / 1 ТБ NVMe SSD*\n\n"

                "🎮 **Игровые ПК:**\n"
                "• Начальный уровень (1080p, киберспорт, средние настройки в новых играх) — от 70 000 до 85 000 руб.\n"
                "  *Конфигурация: Core i3-12100F / GeForce RTX 5050 / 16 ГБ DDR4*\n\n"
                "• Средний уровень (1080p ультра, 1440p средние, стабильный гейминг) — от 85 000 до 120 000 руб.\n"
                "  *Конфигурация: Core i5-12400F / GeForce RTX 5060 / 16-32 ГБ DDR5 / 1 ТБ NVMe SSD*\n\n"
                "• Высокий уровень (1440p ультра, 4K высокие, стриминг) — от 120 000 до 200 000+ руб.\n"
                "  *Конфигурация: Ryzen 7 7700 / GeForce RTX 5070 Ti / 32 ГБ DDR5 / 2 ТБ NVMe SSD*\n\n"

                "💻 **Мини-ПК (компактные решения):**\n"
                "• Базовый мини-ПК (офис, интернет, обучение) — от 20 000 до 30 000 руб.\n"
                "  *Пример: Intel N100 / 8 ГБ / 256 ГБ SSD*\n\n"
                "• Мощный мини-ПК (мультимедиа, лёгкие игры, работа) — от 40 000 до 60 000 руб.\n"
                "  *Пример: Ryzen 7 7840HS с Radeon 780M / 16 ГБ / 512 ГБ SSD (тянет многие игры на минималках!)*\n\n"

                "**⚠️ ВАЖНО: Ситуация на рынке в 2026 году**\n\n"
                "Сейчас наблюдается **глобальный кризис памяти** :\n"
                "• Производители переключились на выпуск чипов для ИИ-серверов, поэтому обычная память в дефиците.\n"
                "• **ОЗУ DDR5 подорожала в 5-7 раз** за полгода: комплект 32 ГБ сейчас стоит 42 000 – 60 000 руб. (летом 2025 было ~9 000 руб.) .\n"
                "• **SSD подорожали вдвое**: 1 ТБ NVMe сейчас ~12 000 руб. (было 5 900 руб.) .\n"
                "• **Видеокарты** тоже дорожают: RTX 5060 уже от 34 000 руб. .\n"
                "• Ожидается дальнейший рост цен на 8-15% в 2026 году .\n\n"

                "**📌 Как отвечать клиентам (строго соблюдай алгоритм):**\n\n"

                "**Шаг 1. Определи категорию компьютера по запросу пользователя (с учётом истории диалога)**\n"
                "- Просмотри всю историю переписки с этим пользователем (переменные `history`). Если в предыдущих сообщениях (включая ответы бота) уже была явно определена категория (например, пользователь ранее указал «игровой», «офисный» и т.д.), то считай, что категория уже известна, и сразу переходи к Шагу 2, не задавая уточняющих вопросов о категории.\n"
                "- Если категория ещё не определена, проанализируй последнее сообщение пользователя по ключевым словам:\n"
                "  * «игровой», «игры», «поиграть», «гейминг», «геймерский», «игровые возможности», «видеокарта для игр» → Игровой ПК.\n"
                "  * «офис», «работа», «1С», «документы», «бухгалтерия», «CRM», «рабочий», «для работы», «офисные задачи» → Офисный/Рабочий ПК.\n"
                "  * «домой», «домашний», «фильмы», «интернет», «мультимедиа», «учеба», «для учёбы», «просмотр видео» → Домашний/Мультимедийный ПК.\n"
                "  * «мини-ПК», «компактный», «маленький», «нано», «настольный мини» → Мини-ПК.\n"
                "- Если ключевые слова не дают однозначной категории или запрос общий (например, «сколько стоит компьютер»), задай уточняющие вопросы: «Для каких задач вам нужен компьютер? Игровых, рабочих или домашних? Это поможет подобрать оптимальную конфигурацию.»\n"
                "- **Важно:** если пользователь ответил на твой уточняющий вопрос (например, ты спросил про категорию, а он ответил «Игровой»), то после получения этого ответа категория считается определённой, и в следующем ответе ты должен использовать информацию из соответствующего раздела (например, «Игровые ПК»), а не задавать тот же вопрос повторно.\n\n"

                "**Шаг 2. После определения категории используй соответствующий раздел с ценами и конфигурациями (см. выше).**\n"
                "• Для **Игровых ПК**: обязательно спроси про разрешение экрана (1080p, 1440p, 4K), какие игры планирует запускать, бюджет.\n"
                "• Для **Офисных ПК**: уточни, какие программы используются (1С, Excel, браузеры, почта), нужно ли много открытых вкладок, работа с базами данных.\n"
                "• Для **Домашних/Мультимедийных**: интересует ли просмотр 4K, работа с фото/видео, нужна ли дискретная видеокарта.\n"
                "• Для **Мини-ПК**: нужна ли портативность, какие задачи, важен ли низкий уровень шума.\n\n"

                "**Шаг 3. Всегда добавляй дисклеймер о ценах:**\n"
                "«Обратите внимание: все приведённые цены являются ориентировочными на текущий момент. Окончательная стоимость рассчитывается индивидуально при оформлении заказа, так как зависит от наличия комплектующих, курса валют и конкретных пожеланий клиента.»\n\n"

                "**Шаг 4. Объясняй причины текущих цен** (кризис памяти, дефицит) – это помогает клиентам понять ситуацию.\n\n"

                "**Шаг 5. Завершай предложением помочь с индивидуальным подбором.**\n\n"

                "**Общий тон:** дружелюбный, экспертный, честный. Если вопрос не по теме, вежливо направляй к тематике группы. Главная цель — помочь клиенту подобрать оптимальное решение под его задачи и бюджет!"
            )
        }
        history.insert(0, system_message)
    history.append({"role": "user", "content": user_message})
    if len(history) > 21:
        history = [history[0]] + history[-20:]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-Title": "VK Bot"
    }
    payload = {
        "model": MODEL,
        "messages": history,
        "temperature": 0.7,
        "max_tokens": 1000
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=30)
            if response.status_code >= 500 and attempt < max_retries - 1:
                logger.warning(f"Ошибка сервера {response.status_code}, попытка {attempt+2} через 2с...")
                time.sleep(2)
                continue
            response.raise_for_status()
            result = response.json()
            assistant_message = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            if not assistant_message or not assistant_message.strip():
                logger.warning("OpenRouter вернул пустой ответ")
                assistant_message = "Извините, сервис вернул пустой ответ. Попробуйте переформулировать вопрос."
            else:
                history.append({"role": "assistant", "content": assistant_message})
                context[user_id] = history
            return assistant_message
        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут, попытка {attempt+1}/{max_retries}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Ошибка запроса: {e}, попытка {attempt+1}/{max_retries}")
        except (KeyError, ValueError) as e:
            logger.error(f"Ошибка парсинга ответа: {e}")
            return "Извините, получен некорректный ответ от сервера."

        if attempt < max_retries - 1:
            time.sleep(2)
        else:
            logger.error("Все попытки исчерпаны")
            return "Извините, сервис временно недоступен. Попробуйте позже."
    return "Произошла неизвестная ошибка."

# ---------- Функции для репостов ----------
def load_reposted():
    """Загружает список уже репостнутых постов из файла."""
    if not os.path.exists(REPOST_FILE):
        return set()
    try:
        with open(REPOST_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except:
        return set()

def save_reposted(post_id):
    """Добавляет ID поста в список репостнутых."""
    reposted = load_reposted()
    reposted.add(post_id)
    with open(REPOST_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(reposted), f, ensure_ascii=False, indent=2)

def get_latest_posts(group_id, count=2):
    """Получает последние посты из группы."""
    try:
        posts = vk.wall.get(owner_id=group_id, count=count)
        return posts['items']
    except Exception as e:
        logger.error(f"Ошибка получения постов из группы {group_id}: {e}")
        return []

def repost_post(post_owner_id, post_id, message=""):
    """Делает репост записи на стену сообщества."""
    object_id = f"wall{post_owner_id}_{post_id}"
    try:
        vk.wall.repost(
            object=object_id,
            message=message,
            group_id=abs(COMMUNITY_ID)  # ID вашей группы (положительное число)
        )
        logger.info(f"Репост успешен: {object_id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка репоста {object_id}: {e}")
        return False

def repost_job():
    """Задача для автоматического репоста (вызывается по расписанию)."""
    logger.info("Запуск задачи репостов")
    reposted = load_reposted()
    
    for group_id in REPOST_SOURCES:
        logger.info(f"Проверяем группу {group_id}")
        posts = get_latest_posts(group_id, count=2)  # берём 2 последних поста
        
        for post in posts:
            post_key = f"{group_id}_{post['id']}"
            if post_key in reposted:
                logger.debug(f"Пост {post_key} уже репостнут, пропускаем")
                continue
            
            # Делаем репост
            success = repost_post(group_id, post['id'], REPOST_MESSAGE)
            if success:
                save_reposted(post_key)
                time.sleep(30)  # пауза между репостами, чтобы не спамить
            else:
                time.sleep(10)
    
    logger.info("Задача репостов завершена")

# ---------- Функции для публикации новостей ----------
def fetch_news(limit=3):
    """Получает новости из RSS 3DNews с изображениями."""
    RSS_URL = "https://3dnews.ru/news/rss"
    feed = feedparser.parse(RSS_URL)
    entries = feed.entries[:limit]
    news_list = []
    
    for entry in entries:
        title = entry.title
        link = entry.link
        summary = entry.summary
        
        # Очищаем summary от HTML-тегов для текста поста
        clean_summary = re.sub(r'<[^>]+>', '', summary)
        clean_summary = clean_summary[:200] + "..." if len(clean_summary) > 200 else clean_summary
        
        # --- ПОИСК ИЗОБРАЖЕНИЯ ---
        image_url = None
        
        # Способ 1: ищем в links (enclosure)
        if hasattr(entry, 'links'):
            for link_item in entry.links:
                if link_item.get('rel') == 'enclosure' and link_item.get('type', '').startswith('image/'):
                    raw_url = link_item.get('href')
                    if raw_url:
                        if raw_url.startswith('/'):
                            image_url = 'https://3dnews.ru' + raw_url
                        else:
                            image_url = raw_url
                        break  # нашли, выходим
        
        # Способ 2: если не нашли в links, ищем в summary
        if not image_url:
            # Ищем первый тег <img> в summary
            img_match = re.search(r'<img[^>]+src="([^">]+)"', summary)
            if img_match:
                raw_url = img_match.group(1)
                if raw_url.startswith('/'):
                    image_url = 'https://3dnews.ru' + raw_url
                else:
                    image_url = raw_url
        
        news_list.append({
            "title": title,
            "link": link,
            "summary": clean_summary,
            "image_url": image_url
        })
    
    return news_list


def post_to_wall(text):
    logger.debug(f"Попытка публикации поста: {text[:50]}...")
    try:
        vk.wall.post(owner_id=COMMUNITY_ID, message=text, from_group=1, random_id=0)
        logger.info("Пост успешно опубликован на стене.")
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")

def job_publish_news():
    logger.info("Запуск задачи публикации новостей")
    try:
        news = fetch_news(limit=2)
        if not news:
            logger.warning("Нет новостей для публикации")
            return
        
        for item in news:
            text = f"🔹 **{item['title']}**\n\n"
            text += f"{item['summary']}\n\n"
            
            if item['image_url']:
                # Добавляем параметр, чтобы ВК думал, что ссылка новая
                image_link = item['image_url'] + "?vk=" + str(int(time.time()))
                text += f"{image_link}\n\n"
            
            text += f"📖 **Читать полностью:** {item['link']}\n\n"
            text += "#новости #технологии"
            
            try:
                vk.wall.post(
                    owner_id=COMMUNITY_ID,
                    message=text,
                    from_group=1,
                    random_id=0
                )
                logger.info("Пост успешно опубликован")
            except Exception as e:
                logger.error(f"Ошибка публикации: {e}")
            
            time.sleep(60)
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в job_publish_news: {e}")

def run_schedule():
    """Запускает планировщик в отдельном потоке."""
    schedule.every().day.at("12:00").do(job_publish_news)
    schedule.every().day.at("10:00").do(repost_job)  # репосты в 10 утра
    schedule.every().day.at("18:00").do(repost_job)  # и в 6 вечера
    # Для теста можно запускать каждые 10 минут:
    # schedule.every(10).minutes.do(job_publish_news)
    while True:
        schedule.run_pending()
        time.sleep(60)

# Запуск планировщика в отдельном потоке
scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
scheduler_thread.start()

# Принудительный тестовый вызов публикации (для проверки)
# logger.info("Тестовый вызов job_publish_news()...")
# job_publish_news()

# ---------- Основной цикл обработки сообщений ----------
logger.info("Бот запущен и ожидает сообщения...")
keyboard = get_main_keyboard()  # создаём один раз, можно переиспользовать


for event in longpoll.listen():
    if event.type == VkEventType.MESSAGE_NEW and event.to_me:
        user_message = event.text.strip()
        user_id = event.user_id

        # Игнорируем пустые сообщения (например, стикеры)
        if not user_message:
            continue

        logger.info(f"Сообщение от {user_id}: {user_message}")

        # 1. Проверка на спам
        if is_flood(user_id):
            logger.info(f"Игнорируем сообщение от {user_id} (флуд)")
            continue

        # 2. Логируем входящее сообщение
        log_conversation(user_id, "user", user_message)

        # 3. Отправляем дисклеймер (если ещё не отправляли)
        if user_id not in disclaimer_sent:
            disclaimer = (
                "⚠️ Это автоматический помощник группы «КОМПЬЮТЕРНЫЕ РЕШЕНИЯ - Сборка ПК на заказ». "
                "Я не могу дать точные данные о ценах и наличии, но могу записать ваши пожелания "
                "и передать их менеджеру. Для точного расчёта и оформления заказа свяжитесь с нами."
            )
            send_message(user_id, disclaimer, keyboard)
            log_conversation(user_id, "assistant", disclaimer)
            disclaimer_sent.add(user_id)
            # После отправки дисклеймера не нужно сразу обрабатывать основное сообщение?
            # Лучше продолжить и ответить на исходный запрос.

        # 4. Обрабатываем нажатия на кнопки (категории)
        if user_message == '🕹️ Игровые ПК':
            answer = "Вы выбрали категорию **Игровые ПК**. Расскажите, в какие игры планируете играть и какой бюджет? Например: «хочу играть в Cyberpunk 2077 в 1080p, бюджет до 80 000 руб»."
        elif user_message == '💼 Офисные ПК':
            answer = "Вы выбрали категорию **Офисные ПК**. Какие программы планируете использовать (1С, Excel, браузеры)? Сколько сотрудников будет работать?"
        elif user_message == '🏠 Домашние/Мультимедиа':
            answer = "Вы выбрали категорию **Домашний/Мультимедийный ПК**. Для каких задач: интернет, фильмы 4K, фото/видеомонтаж?"
        elif user_message == '📦 Мини-ПК':
            answer = "Вы выбрали категорию **Мини-ПК**. Важна ли портативность? Будете ли использовать для игр?"
        elif user_message == '📞 Связаться с менеджером':
            answer = "Оставьте ваш номер телефона, и менеджер свяжется с вами в ближайшее время. Или напишите нам напрямую: https://vk.me/имя_группы"
        elif user_message == 'ℹ️ О группе':
            answer = (
                "Группа «КОМПЬЮТЕРНЫЕ РЕШЕНИЯ - Сборка ПК на заказ» занимается профессиональной сборкой компьютеров с 2015 года. "
                "Мы предлагаем:\n• Игровые ПК\n• Офисные рабочие станции\n• Домашние мультимедиа\n• Мини-ПК\n• Апгрейд и ремонт\n\n"
                "Свяжитесь с нами для индивидуального подбора!"
            )
        else:
            # 5. Если это не команда, передаём запрос в OpenRouter
            answer = ask_openrouter_with_context(user_id, user_message)

        # 6. Отправляем ответ с клавиатурой
        send_message(user_id, answer, keyboard)

        # 7. Логируем ответ бота
        log_conversation(user_id, "assistant", answer)
