import os
import asyncio
import smtplib
import ssl
import mimetypes
import uuid
from datetime import timedelta
from email.message import EmailMessage
import httpx  # Используется для REST API Яндекса и загрузки Gist
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPoll, MessageEntityTextUrl

# --- НАСТРОЙКИ ИЗ SECRETS ---
API_ID = int(os.getenv('TG_API_ID'))
API_HASH = os.getenv('TG_API_HASH')
SESSION_STRING = os.getenv('TG_SESSION') 

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
SENDER_EMAIL = os.getenv('MAIL_SENDER')
SENDER_PASS = os.getenv('MAIL_PASS')
RECEIVER_EMAIL = os.getenv('MAIL_RECEIVER')
MAX_EMAIL_SIZE = 24 * 1024 * 1024 
YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN')

# Прямая ссылка на RAW-версию твоего Gist со списком VPN-каналов
VPN_GIST_URL = os.getenv('VPN_GIST_URL', '')

RAM_PATH = os.path.join(os.getcwd(), 'temp_media')
os.makedirs(RAM_PATH, exist_ok=True)

LARGE_MEDIA_PATH = os.path.join(os.getcwd(), 'saved_large_media')
os.makedirs(LARGE_MEDIA_PATH, exist_ok=True)

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
mail_queue = asyncio.Queue()

async def fetch_vpn_channels_list():
    """Загружает список VPN-каналов из GitHub Gist"""
    if not VPN_GIST_URL:
        print("ℹ️ VPN_GIST_URL не задан. Все письма будут отправляться с темой Telegram.")
        return set()
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            res = await http_client.get(VPN_GIST_URL)
            if res.status_code == 200:
                lines = res.text.splitlines()
                # Очищаем строки, убираем @, пустые строки и пробелы
                vpn_list = set()
                for line in lines:
                    item = line.strip().lstrip('@').lower()
                    if item:
                        vpn_list.add(item)
                print(f"📋 Загружен список VPN-каналов ({len(vpn_list)} шт.) из Gist")
                return vpn_list
            else:
                print(f"⚠️ Ошибка загрузки Gist: Статус {res.status_code}")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить список VPN-каналов из Gist: {e}")
    
    return set()

def read_file_sync(path):
    with open(path, "rb") as f:
        return f.read()

async def upload_to_yandex_disk(local_path, filename):
    if not YANDEX_DISK_TOKEN:
        print("❌ Ошибка: YANDEX_DISK_TOKEN не задан в secrets!")
        return None

    unique_filename = f"{uuid.uuid4().hex[:8]}_{filename}"
    headers = {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}
    timeout = httpx.Timeout(900.0, connect=90.0)
    
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        try:
            get_upload_url = "https://cloud-api.yandex.net/v1/disk/resources/upload"
            params = {"path": f"/{unique_filename}", "overwrite": "true"}
            
            url_res = await http_client.get(get_upload_url, params=params, headers=headers)
            if url_res.status_code != 200:
                return None
                
            upload_url = url_res.json().get("href")
            if not upload_url:
                return None

            file_content = await asyncio.to_thread(read_file_sync, local_path)
            upload_res = await http_client.put(upload_url, content=file_content)
                
            if upload_res.status_code not in (201, 202):
                return None
            
            publish_url = "https://cloud-api.yandex.net/v1/disk/resources/publish"
            pub_res = await http_client.put(publish_url, params={"path": f"/{unique_filename}"}, headers=headers)
            
            if pub_res.status_code == 200:
                meta_url = "https://cloud-api.yandex.net/v1/disk/resources"
                meta_res = await http_client.get(meta_url, params={"path": f"/{unique_filename}"}, headers=headers)
                if meta_res.status_code == 200:
                    return meta_res.json().get("public_url")
            return None
        except Exception as e:
            print(f"❌ Ошибка Яндекс Диска: {e!r}")
            return None

async def send_mail_worker():
    while not mail_queue.empty():
        subject, html_body, files = await mail_queue.get()
        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = SENDER_EMAIL
            msg['To'] = RECEIVER_EMAIL
            msg.set_content("HTML format required.")
            msg.add_alternative(html_body, subtype='html')
            
            for f_data, f_name, cid in files:
                ctype, _ = mimetypes.guess_type(f_name)
                ctype = ctype or 'application/octet-stream'
                maintype, subtype = ctype.split('/', 1)
                msg.get_payload()[1].add_related(
                    f_data, 
                    maintype=maintype, 
                    subtype=subtype, 
                    filename=f_name, 
                    cid=f"<{cid}>"
                )

            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
                server.login(SENDER_EMAIL, SENDER_PASS)
                server.send_message(msg)
            
            print(f"✅ Отправлено: {subject}")
            
            if not mail_queue.empty():
                await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ Ошибка SMTP: {e}")
        finally:
            mail_queue.task_done()

def get_html_text(msg):
    if not msg: return ""
    raw_text = msg.message or ""
    if not raw_text: return ""

    if msg.entities:
        sorted_entities = sorted(msg.entities, key=lambda e: e.offset, reverse=True)
        html_text = raw_text
        for ent in sorted_entities:
            if isinstance(ent, MessageEntityTextUrl):
                start = ent.offset
                end = ent.offset + ent.length
                text_word = html_text[start:end]
                html_link = f'<a href="{ent.url}" style="color: #0088cc; text-decoration: underline;">{text_word}</a>'
                html_text = html_text[:start] + html_link + html_text[end:]
        return html_text.replace('\n', '<br>')
        
    html_text = msg.text_html if hasattr(msg, 'text_html') and msg.text_html else raw_text
    return html_text.replace('\n', '<br>')

async def process_messages(messages, chat_entity, vpn_list, mark_read=False):
    if not messages: return
    first_msg = messages[0]
    chat_title = chat_entity.title
    
    # Определение темы письма (VPN или Telegram)
    chat_username = (getattr(chat_entity, 'username', '') or '').lower()
    chat_id_str = str(chat_entity.id)
    
    if chat_username in vpn_list or chat_id_str in vpn_list:
        email_subject = "VPN"
    else:
        email_subject = "Telegram"

    if chat_username:
        base_url = f"https://t.me/{chat_username}"
    else:
        base_url = f"https://t.me/c/{chat_entity.id}"

    msg_with_text = next((m for m in messages if m.message), None)
    formatted_text = get_html_text(msg_with_text) if msg_with_text else ""
    
    files = []
    media_html = ""
    poll_html = ""

    total_size = sum(getattr(msg.file, 'size', 0) for msg in messages if msg.media)

    for msg in messages:
        if not msg.media:
            continue
            
        if isinstance(msg.media, MessageMediaPoll):
            poll = msg.media.poll
            poll_type = "📊 Опрос" if not poll.quiz else "💡 Викторина"
            question = poll.question.text if hasattr(poll.question, 'text') else str(poll.question)
            
            answers_list = ""
            for answer in poll.answers:
                answer_text = answer.text.text if hasattr(answer.text, 'text') else str(answer.text)
                answers_list += f'<li style="margin-bottom: 5px;">🔹 {answer_text}</li>'
            
            poll_html += f"""
            <div style="background-color: #f4f7f9; border-left: 4px solid #0088cc; padding: 12px; margin-top: 15px; border-radius: 4px;">
                <strong style="color: #0088cc;">{poll_type}:</strong> <span style="font-size: 15px; font-weight: bold;">{question}</span>
                <ul style="list-style-type: none; padding-left: 5px; margin-top: 10px; margin-bottom: 0;">
                    {answers_list}
                </ul>
            </div>
            """
            continue

        file_size = getattr(msg.file, 'size', 0)
        
        if total_size >= MAX_EMAIL_SIZE or file_size >= MAX_EMAIL_SIZE:
            path = await msg.download_media(file=LARGE_MEDIA_PATH)
            if path and os.path.exists(path):
                f_name = os.path.basename(path)
                print(f"💾 Файл {f_name} превысил лимит. Выгружаем на Яндекс Диск...")
                yandex_url = await upload_to_yandex_disk(path, f_name)
                
                if yandex_url:
                    media_html += f'<br><p>📦 <b>Большой файл (загружен на Яндекс Диск):</b> <a href="{yandex_url}">{f_name}</a></p>'
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    media_html += f'<br><p>📦 <b>Большой файл (ошибка Я.Диска, сохранен локально):</b> <code>{f_name}</code></p>'
        else:
            path = await msg.download_media(file=RAM_PATH)
            if path and os.path.exists(path):
                f_name = os.path.basename(path)
                cid = str(uuid.uuid4())
                with open(path, 'rb') as f:
                    f_data = f.read()
                
                files.append((f_data, f_name, cid))
                os.remove(path)
                
                if f_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    media_html += f'<br><img src="cid:{cid}" style="max-width: 100%;"><br>'
                elif f_name.lower().endswith(('.mp3', '.ogg', '.wav', '.m4a')):
                    media_html += f'<br><p>🎵 Аудиофайл: {f_name}</p>'
                else:
                    media_html += f'<br><p>📎 Вложение: {f_name}</p>'
    
    local_time = first_msg.date + timedelta(hours=5)
    pub_date = local_time.strftime("%d.%m.%Y %H:%M:%S")
    
    reply_to_html = ""
    quoted_post_html = ""
    if first_msg.reply_to and first_msg.reply_to.reply_to_msg_id:
        reply_msg_id = first_msg.reply_to.reply_to_msg_id
        reply_to_html = f' | <a href="{base_url}/{reply_msg_id}" style="color: #0056b3; text-decoration: none;">в ответ</a>'
        
        try:
            parent_msg = await client.get_messages(chat_entity, ids=reply_msg_id)
            if parent_msg:
                parent_text = get_html_text(parent_msg)
                if not parent_text and parent_msg.media:
                    parent_text = "<i>[Медиавложение]</i>"
                
                quoted_post_html = f"""
                <blockquote style="margin: 0 0 15px 0; padding: 5px 0 5px 12px; border-left: 3px solid #0088cc; color: #555; background-color: #f9f9f9;">
                    <small style="color: #0088cc; font-weight: bold;">В ответ на post от {parent_msg.date.strftime('%d.%m.%Y')}:</small><br>
                    {parent_text}
                </blockquote>
                """
        except Exception as e:
            print(f"⚠️ Не удалось получить родительский пост #{reply_msg_id}: {e}")
    
    post_url = f"{base_url}/{first_msg.id}"
    
    html_body = f"""
    <html>
    <body>
        <h3>{chat_title}</h3>
        <p style="color: #666; font-size: 14px;">🕒 {pub_date}{reply_to_html}</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 15px 0;">
        <div style="margin-bottom: 20px;">
            {quoted_post_html}
            {formatted_text}
        </div>
        {poll_html}
        {media_html}
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;">
        <small style="color: #999;">Ссылка на post: <a href="{post_url}">{post_url}</a></small>
    </body>
    </html>
    """
    
    # Отправляем в очередь с динамической темой
    await mail_queue.put((email_subject, html_body, files))
    
    if mark_read:
        await client.send_read_acknowledge(first_msg.chat_id, max_id=max(m.id for m in messages))

async def main():
    print("🚀 Подключение к Telegram...")
    vpn_list = await fetch_vpn_channels_list()
    
    async with client:
        async for dialog in client.iter_dialogs():
            if dialog.is_channel and dialog.unread_count > 0:
                print(f"📥 Читаем {dialog.name}: {dialog.unread_count} новых")
                chat_entity = await client.get_entity(dialog.id)
                messages = await client.get_messages(dialog.id, limit=dialog.unread_count)
                
                grouped = {}
                ungrouped = []
                for m in reversed(messages):
                    if m.grouped_id:
                        grouped.setdefault(m.grouped_id, []).append(m)
                    else:
                        ungrouped.append([m])
                
                all_batches = list(grouped.values()) + ungrouped
                for i, batch in enumerate(all_batches):
                    should_mark = (i == len(all_batches) - 1)
                    await process_messages(batch, chat_entity, vpn_list, mark_read=should_mark)
    
    print("🔌 Соединение с Telegram закрыто.")

    if not mail_queue.empty():
        print(f"📧 Начинаем отправку писем ({mail_queue.qsize()} шт.)...")
        await send_mail_worker()
    else:
        print("📭 Новых постов не найдено.")
            
    print("💤 Всё готово. Скрипт завершен.")

if __name__ == '__main__':
    asyncio.run(main())
