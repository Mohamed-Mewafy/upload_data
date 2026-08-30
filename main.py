import os
import time
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
import yt_dlp
import internetarchive as ia
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ACCESS_KEY = os.environ.get("IA_ACCESS_KEY")
SECRET_KEY = os.environ.get("IA_SECRET_KEY")

MAX_CONCURRENT_WORKERS = 3
log_lock = Lock()

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def log(msg):
    with log_lock:
        print(msg, flush=True)

def get_video_link_with_browser(embed_url, item_id):
    """استخراج رابط الفيديو المباشر بسرعات متناهية مع تأمين التوازي"""
    short_id = str(item_id)[:8]
    log(f"🌐 [{short_id}] تجربة الرابط: {embed_url}")
    extracted_url = None
    
    try:
        # تشغيل Playwright مع التوازي بأمان
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage", 
                    "--no-sandbox", 
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                extra_http_headers={"Referer": embed_url},
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()
            
            # حجب الصور والخطوط لتقليل التحميل وتسريع الفحص
            def block_heavy_resources(route):
                if route.request.resource_type in ["image", "stylesheet", "font"]:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy_resources)
            
            def check_url(url):
                nonlocal extracted_url
                url_lower = url.lower()
                if any(ext in url_lower for ext in ['.m3u8', '.mp4', 'video/mp4']) and not any(ign in url_lower for ign in ['chunk', 'ads', 'seg', 'analytics', 'googlevideo']):
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
    """تحميل صاروخي بأقصى سرعة شبكة ممكّنة"""
    short_id = str(record_id)[:8]
    output_path = f"{record_id}.mp4"
    log(f"📥 [{short_id}] بدء التحميل السريع جداً...")
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'retries': 10,
        'fragment_retries': 20,
        'skip_unavailable_fragments': True,
        'concurrent_fragment_downloads': 8,
        'http_headers': {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": embed_url
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
            
        if os.path.exists(output_path):
            file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            if file_size_mb > 2:
                log(f"📦 [{short_id}] اكتمل التحميل المحلي بنجاح ({file_size_mb:.1f} MB)")
                return output_path
            else:
                log(f"⚠️ [{short_id}] الملف غير صالح (أقل من 2MB)")
                if os.path.exists(output_path):
                    os.remove(output_path)
    except Exception as e:
        log(f"❌ [{short_id}] فشل التحميل المحلي: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
            
    return None

def upload_to_archive(file_path, record_id):
    """رفع الملف إلى Archive.org"""
    short_id = str(record_id)[:8]
    identifier = f"cimaspace-item-{record_id}"
    log(f"🚀 [{short_id}] جاري الرفع لـ Archive...")
    
    metadata = {
        'mediatype': 'movies',
        'collection': 'opensource_movies',
        'title': f"Media Item {record_id}",
        'description': 'CimaSpace Video Stream'
    }

    try:
        r = ia.upload(
            identifier,
            files=[file_path],
            metadata=metadata,
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            retries=3,
            verbose=False
        )
        
        if r and r[0].status_code == 200:
            archive_embed_url = f"https://archive.org/embed/{identifier}"
            log(f"✅ [{short_id}] تم الرفع بنجاح! الرابط: {archive_embed_url}")
            return archive_embed_url
        else:
            log(f"⚠️ [{short_id}] فشل استجابة الأرشيف")
            return None
    except Exception as e:
        log(f"❌ [{short_id}] خطأ أثناء الرفع: {e}")
        return None

def update_status(table_name, record_id, archive_url):
    """تحديث حالة الفيلم في Supabase"""
    short_id = str(record_id)[:8]
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "watch_url": archive_url
        }).eq("id", record_id).execute()
        log(f"✨ [{short_id}] تم تحديث Supabase!")
    except Exception as e:
        log(f"❌ [{short_id}] خطأ في التحديث: {e}")

def process_single_item(item, table_name, url_column, title_column):
    record_id = item.get("id")
    if not record_id:
        return False
        
    short_id = str(record_id)[:8]
    title = item.get(title_column) or "Unnamed Video"
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
        return False

    log(f"\n🎬 [{short_id}] بدء المعالجة: {title}")
    
    for link_index, current_url in enumerate(urls_to_try, 1):
        log(f"🔗 [{short_id}] تجربة الرابط ({link_index}/{len(urls_to_try)})...")
        
        direct_url, embed_src = get_video_link_with_browser(current_url, record_id)
        if direct_url:
            local_file = download_video_temporarily(direct_url, embed_src, record_id)
            if local_file and os.path.exists(local_file):
                archive_url = upload_to_archive(local_file, record_id)
                
                if os.path.exists(local_file):
                    os.remove(local_file)

                if archive_url:
                    update_status(table_name, record_id, archive_url)
                    log(f"🎉 [{short_id}] اكتملت العملية بنجاح للفيلم: {title}\n")
                    return True
                
        log(f"🔄 [{short_id}] الانتقال للرابط التالي...")
        
    log(f"❌ [{short_id}] فشلت جميع الروابط المتاحة للفيلم: {title}\n")
    return False

def process_table_parallel(table_name, url_column, title_column="title", limit=10):
    log(f"\n==========================================")
    log(f"📂 فحص الجدول: {table_name}")
    log(f"==========================================")
    
    try:
        response = supabase.table(table_name).select(f"id, {title_column}, {url_column}, direct_links").eq("is_uploaded", False).limit(limit).execute()
        items = response.data
    except Exception as e:
        log(f"❌ خطأ في جلب البيانات من {table_name}: {e}")
        return

    if not items:
        log(f"🎉 لا توجد عناصر جديدة في جدول {table_name}.")
        return

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        futures = [
            executor.submit(process_single_item, item, table_name, url_column, title_column) 
            for item in items
        ]
        
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"❌ خطأ غير متوقع في المهمة: {e}")

def main():
    process_table_parallel("movies_cima", "watch_url", "title", limit=1000)
    process_table_parallel("arabic_movies", "watch_url", "title", limit=10)
    process_table_parallel("tv_series", "watch_url", "title", limit=10)
    process_table_parallel("episodes_cima", "watch_url", "title", limit=10)
    
    log("\n🏁 انتهت كل العمليات!")

if __name__ == "__main__":
    main()
