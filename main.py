import os
import time
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

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_video_link_with_browser(embed_url):
    """استخراج رابط الفيديو المباشر باستخدام Playwright"""
    print(f"🌐 [صيد الرابط] تجربة: {embed_url}", flush=True)
    extracted_url = None
    
    try:
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
                extra_http_headers={"Referer": "https://cimaspace.site/"},
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()
            
            def check_url(url):
                nonlocal extracted_url
                if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'video/mp4']) and 'chunk' not in url and 'ads' not in url and 'seg' not in url:
                    if not extracted_url:
                        extracted_url = url
                        print(f"🎯 [تم صيد الرابط المباشر]: {url}", flush=True)

            page.on("request", lambda req: check_url(req.url))
            page.on("response", lambda res: check_url(res.url))
            
            try:
                # زيادة وقت التايم آوت إلى 60 ثانية لتفادي Timeout Error
                page.goto(embed_url, timeout=60000, wait_until="domcontentloaded")
                for i in range(3):
                    try:
                        page.evaluate("""() => {
                            const elements = document.querySelectorAll('video, .play-btn, [class*="play"], [id*="play"], .jw-display-icon-container, iframe');
                            elements.forEach(el => el.click());
                        }""")
                    except:
                        pass
                    
                    try:
                        page.mouse.click(640, 360)
                    except:
                        pass
                    
                    time.sleep(3)
                    if extracted_url:
                        break
            except Exception as e:
                print(f"⚠️ تجاوز الرابط بسبب خطأ في التحميل/التايم آوت: {e}", flush=True)
                
            browser.close()
    except Exception as e:
        print(f"❌ خطأ في محرك Playwright: {e}", flush=True)
        
    return extracted_url

def download_video_temporarily(video_url, record_id):
    """تحميل الفيديو مؤقتاً بأقصى سرعة"""
    output_path = f"{record_id}.mp4"
    print(f"📥 [تحميل مؤقت]: {record_id}...", flush=True)
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'concurrent_fragment_downloads': 8,
        'http_headers': {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://cimaspace.site/"
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
            
        if os.path.exists(output_path):
            file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            if file_size_mb > 2:
                print(f"📦 تم التحميل بنجاح ({file_size_mb:.2f} MB)", flush=True)
                return output_path
            else:
                print(f"❌ الملف صغير جداً ({file_size_mb:.2f} MB)، الرابط تالف.", flush=True)
                os.remove(output_path)
    except Exception as e:
        print(f"❌ فشل التحميل المحلي: {e}", flush=True)
        if os.path.exists(output_path):
            os.remove(output_path)
            
    return None

def upload_to_archive(file_path, record_id):
    """رفع الملف إلى Archive.org بمعرف UUID فقط وبدون شريط تقدم متضارب"""
    identifier = f"{record_id}"
    file_name = f"{record_id}.mp4"
    print(f"🚀 [جاري الرفع لـ Archive]: ID [{identifier}]...", flush=True)
    
    metadata = {
        'mediatype': 'movies',
        'collection': 'opensource_movies',
        'title': f"{record_id}",
        'description': 'Media Content'
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
            archive_download_url = f"https://archive.org/download/{identifier}/{file_name}"
            print(f"✅ [تم الرفع وسيعمل المشغل بنجاح!]: {archive_download_url}", flush=True)
            return archive_download_url
        else:
            print(f"⚠️ فشل استجابة الأرشيف للرفع.", flush=True)
            return None
    except Exception as e:
        print(f"❌ خطأ أثناء الرفع إلى Archive: {e}", flush=True)
        return None

def update_status(table_name, record_id, archive_url):
    """تحديث قاعدة البيانات برابط الأرشيف النهائي"""
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "watch_url": archive_url
        }).eq("id", record_id).execute()
        print(f"🔄 تم تحديث حالة الرفع في جدول [{table_name}] للـ ID: {record_id}", flush=True)
    except Exception as e:
        print(f"❌ خطأ أثناء التحديث في Supabase: {e}", flush=True)

def process_single_item(item, table_name, url_column, title_column):
    """معالجة عنصر واحد بالكامل"""
    record_id = item.get("id")
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
        print(f"⚠️ لا توجد روابط صالحة للعنصر: {title}", flush=True)
        return False

    print(f"\n🎬 معالجة: {title} (ID: {record_id})", flush=True)
    
    for link_index, current_url in enumerate(urls_to_try, 1):
        print(f"🔗 محاولة الرابط ({link_index}/{len(urls_to_try)}) للفيلم ID: {record_id}...", flush=True)
        
        direct_url = get_video_link_with_browser(current_url)
        if direct_url:
            local_file = download_video_temporarily(direct_url, record_id)
            if local_file and os.path.exists(local_file):
                archive_url = upload_to_archive(local_file, record_id)
                
                if os.path.exists(local_file):
                    os.remove(local_file)

                if archive_url:
                    update_status(table_name, record_id, archive_url)
                    return True
                
        print(f"⚠️ فشل الرابط الحالي للـ ID: {record_id}، تجربة الرابط التالي...", flush=True)
        
    print(f"❌ فشلت كل الروابط المتاحة للفيلم ID: {record_id}", flush=True)
    return False

def process_table_parallel(table_name, url_column, title_column="title", limit=10):
    """جلب العناصر وتنفيذ المعالجة بالتوازي عبر ThreadPoolExecutor"""
    print(f"\n========================================", flush=True)
    print(f"📂 فحص الجدول: {table_name}", flush=True)
    print(f"========================================", flush=True)
    
    try:
        response = supabase.table(table_name).select(f"id, {title_column}, {url_column}, direct_links").eq("is_uploaded", False).limit(limit).execute()
        items = response.data
    except Exception as e:
        print(f"❌ خطأ في جلب البيانات من {table_name}: {e}", flush=True)
        return

    if not items:
        print(f"🎉 لا توجد عناصر جديدة في جدول {table_name}.", flush=True)
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
                print(f"❌ حدث خطأ غير متوقع في إحدى المهام بالتوازي: {e}", flush=True)

def main():
    process_table_parallel("movies_cima", "watch_url", "title", limit=10)
    process_table_parallel("arabic_movies", "watch_url", "title", limit=10)
    process_table_parallel("tv_series", "watch_url", "title", limit=10)
    process_table_parallel("episodes_cima", "watch_url", "title", limit=10)
    
    print("\n🏁 انتهت كل المهام بنجاح وبأعلى سرعة!", flush=True)

if __name__ == "__main__":
    main()
