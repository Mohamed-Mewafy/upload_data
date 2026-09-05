import os
import time
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
import requests
import yt_dlp
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
from pyrogram import Client as TelegramClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# ⚙️ المتغيرات البيئية ومفاتيح الاتصال
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "21631130")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "e0617a3a50796aa895af3a4ba03ba748")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8602724340:AAHum6vJ6hRdQDii5VATwlAoPguWNumvjDs")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003891077968")

MAX_CONCURRENT_WORKERS = 2  # تقليل عدد العمال لتجنب قيود تليجرام (FloodWait)
log_lock = Lock()
processed_ids_lock = Lock()

in_memory_locked_ids = set()

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("❌ خطأ: بيانات التليجرام TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير معرفة.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# تهيئة عميل تليجرام باستخدام Pyrogram
tg_app = TelegramClient(
    "telegram_uploader_bot",
    api_id=int(TELEGRAM_API_ID),
    api_hash=TELEGRAM_API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN
)

def log(msg):
    with log_lock:
        print(msg, flush=True)

def try_lock_record(table_name, record_id):
    """حجز العنصر بالذاكرة وفي Supabase لمنع التكرار"""
    with processed_ids_lock:
        if record_id in in_memory_locked_ids:
            return False
        in_memory_locked_ids.add(record_id)

    try:
        supabase.table(table_name).update({"is_processing": True}).eq("id", record_id).execute()
    except Exception:
        pass
    return True

def unlock_record_on_failure(table_name, record_id):
    """إلغاء الحجز في حال الفشل لإتاحة المحاولة مستقبلاً"""
    with processed_ids_lock:
        in_memory_locked_ids.discard(record_id)
    try:
        supabase.table(table_name).update({"is_processing": False}).eq("id", record_id).execute()
    except Exception:
        pass

# ==========================================
# ✈️ محرك الرفع إلى Telegram
# ==========================================

def upload_to_telegram(file_path, record_id, title="Video", category_name="General"):
    """رفع الفيديو إلى قناة التليجرام واستخراج رابط الرسالة"""
    short_id = str(record_id)[:8]
    caption = f"🎬 **{title}**\n📂 القسم: #{category_name.replace(' ', '_')}\n🆔 UUID: `{record_id}`"

    log(f"🚀 [{short_id}] جاري رفع الفيديو إلى التليجرام...")

    try:
        with tg_app:
            msg = tg_app.send_video(
                chat_id=int(TELEGRAM_CHAT_ID),
                video=file_path,
                caption=caption,
                supports_streaming=True
            )
            
            # بناء رابط الرسالة
            if msg.link:
                telegram_url = msg.link
            else:
                chat_str = str(TELEGRAM_CHAT_ID).replace("-100", "")
                telegram_url = f"https://t.me/c/{chat_str}/{msg.id}"

            log(f"✅ [{short_id}] تم الرفع إلى تليجرام بنجاح: {telegram_url}")
            return telegram_url

    except Exception as e:
        log(f"❌ [{short_id}] خطأ أثناء الرفع إلى Telegram: {e}")
        return None

# ==========================================
# 🌐 محرك الاستخراج والتحميل
# ==========================================

def get_video_link_with_browser(embed_url, item_id):
    """استخراج رابط الفيديو المباشر عبر Playwright"""
    short_id = str(item_id)[:8]
    log(f"🌐 [{short_id}] تجربة الرابط: {embed_url}")
    extracted_url = None
    
    if any(domain in embed_url.lower() for domain in ["archive.org", "t.me", "telegram.org"]):
        log(f"⚠️ [{short_id}] تخطي رابط غير صالح أو مكرر: {embed_url}")
        return None, embed_url

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage", 
                    "--no-sandbox", 
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--blink-settings=imagesEnabled=false"
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                extra_http_headers={"Referer": embed_url},
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()
            
            def check_url(url):
                nonlocal extracted_url
                url_lower = url.lower()
                if any(ext in url_lower for ext in ['.m3u8', '.mp4', 'video/mp4']) and not any(ign in url_lower for ign in ['archive.org', 't.me', 'chunk', 'ads', 'seg', 'analytics', 'googlevideo']):
                    if not extracted_url:
                        extracted_url = url
                        log(f"🎯 [{short_id}] تم صيد الرابط المباشر")

            page.on("request", lambda req: check_url(req.url))
            page.on("response", lambda res: check_url(res.url))
            
            try:
                page.goto(embed_url, timeout=12000, wait_until="domcontentloaded")
                try:
                    page.evaluate("""() => {
                        const elements = document.querySelectorAll('video, .play-btn, [class*="play"], [id*="play"], .jw-display-icon-container, iframe');
                        elements.forEach(el => el.click());
                    }""")
                except Exception:
                    pass
                
                for _ in range(4):
                    if extracted_url:
                        break
                    time.sleep(0.5)
            except Exception:
                log(f"⚠️ [{short_id}] تجاوز الرابط (تخطى المهلة 12s)")
                
            browser.close()
    except Exception as e:
        log(f"❌ [{short_id}] خطأ في محرك Playwright: {e}")
        
    return extracted_url, embed_url

def download_video_temporarily(video_url, embed_url, record_id):
    """تحميل سريع ومؤقت لنقل الملف إلى Telegram"""
    short_id = str(record_id)[:8]
    output_path = f"{record_id}.mp4"
    log(f"📥 [{short_id}] بدء التحميل السريع...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": embed_url if embed_url else "https://google.com/",
    }

    try:
        with requests.get(video_url, headers=headers, stream=True, timeout=15) as r:
            if r.status_code == 200:
                with open(output_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(output_path) and (os.path.getsize(output_path) / (1024 * 1024)) > 2:
                    log(f"📦 [{short_id}] اكتمل التحميل المباشر بنجاح!")
                    return output_path
    except Exception:
        pass

    ydl_opts = {
        'format': 'bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1][ext=mp4]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'http_headers': headers,
        'socket_timeout': 15,
        'source_address': '0.0.0.0',
        'ignoreerrors': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
            
        if os.path.exists(output_path):
            file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            if file_size_mb > 2:
                log(f"📦 [{short_id}] اكتمل التحميل ({file_size_mb:.1f} MB)")
                return output_path
            else:
                if os.path.exists(output_path):
                    os.remove(output_path)
    except Exception as e:
        log(f"❌ [{short_id}] فشل التحميل المحلي: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
            
    return None

def update_status(table_name, record_id, watch_url):
    """تحديث قاعدة البيانات برابط رسالة التليجرام"""
    short_id = str(record_id)[:8]
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "is_processing": False,
            "watch_url": watch_url
        }).eq("id", record_id).execute()
        log(f"✨ [{short_id}] تم التحديث بنجاح في Supabase!")
    except Exception:
        try:
            supabase.table(table_name).update({
                "is_uploaded": True,
                "watch_url": watch_url
            }).eq("id", record_id).execute()
            log(f"✨ [{short_id}] تم التحديث بنجاح!")
        except Exception as e:
            log(f"❌ [{short_id}] خطأ في التحديث: {e}")

# ==========================================
# ⚙️ معالجة العناصر والتشغيل
# ==========================================

def process_single_item(item, table_name, url_column, title_column, category_name):
    record_id = item.get("id")
    if not record_id:
        return False

    if not try_lock_record(table_name, record_id):
        return False

    short_id = str(record_id)[:8]
    title = item.get(title_column) or str(record_id)

    main_watch_url = item.get(url_column)
    direct_links_json = item.get("direct_links") or {}
    
    urls_to_try = []
    if main_watch_url:
        urls_to_try.append(main_watch_url)
        
    if isinstance(direct_links_json, dict):
        streaming_list = direct_links_json.get("streaming_links", [])
        if isinstance(streaming_list, list):
            for alt_url in streaming_list:
                if alt_url and alt_url not in urls_to_try:
                    urls_to_try.append(alt_url)

    if not urls_to_try:
        log(f"⚠️ [{short_id}] لا توجد روابط صالحة للفيلم: {title}")
        unlock_record_on_failure(table_name, record_id)
        return False

    log(f"\n🎬 [{short_id}] بدء المعالجة: {title} | القسم: [{category_name}]")
    
    for link_index, current_url in enumerate(urls_to_try, 1):
        log(f"🔗 [{short_id}] تجربة الرابط ({link_index}/{len(urls_to_try)})...")
        
        direct_url, embed_src = get_video_link_with_browser(current_url, record_id)
        if direct_url:
            local_file = download_video_temporarily(direct_url, embed_src, record_id)
            if local_file and os.path.exists(local_file):
                
                # الرفع إلى تليجرام
                tg_watch_url = upload_to_telegram(local_file, record_id, title=title, category_name=category_name)
                
                if os.path.exists(local_file):
                    os.remove(local_file)

                if tg_watch_url:
                    update_status(table_name, record_id, tg_watch_url)
                    log(f"🎉 [{short_id}] مكتمل بنجاح: {title}\n")
                    return True
                
        log(f"🔄 [{short_id}] الانتقال للرابط التالي...")
        
    log(f"❌ [{short_id}] فشلت جميع الروابط للفيلم: {title}\n")
    unlock_record_on_failure(table_name, record_id)
    return False

def process_table_parallel(table_name, url_column, title_column="title", category_name="General", limit=200):
    log(f"\n==========================================")
    log(f"📂 فحص الجدول: {table_name} | القسم: {category_name}")
    log(f"==========================================")
    
    try:
        response = supabase.table(table_name).select("*").eq("is_uploaded", False).limit(limit).execute()
        items = response.data
    except Exception as e:
        log(f"❌ خطأ في جلب البيانات من {table_name}: {e}")
        return

    if not items:
        log(f"🎉 لا توجد عناصر جديدة في جدول {table_name}.")
        return

    valid_items = [item for item in items if item.get("id") and item.get("id") not in in_memory_locked_ids]

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        futures = [
            executor.submit(process_single_item, item, table_name, url_column, title_column, category_name) 
            for item in valid_items
        ]
        
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"❌ خطأ غير متوقع في المهمة: {e}")

def main():
    process_table_parallel("movies_cima", "watch_url", "title", category_name="Foreign Movies", limit=100)
    process_table_parallel("arabic_movies", "watch_url", "title", category_name="Arabic Movies", limit=50)
    process_table_parallel("tv_series", "watch_url", "title", category_name="TV Series", limit=30)
    process_table_parallel("episodes_cima", "watch_url", "title", category_name="Episodes", limit=20)
    
    log("\n🏁 انتهت كل العمليات لهذا الشوط!")

if __name__ == "__main__":
    main()
