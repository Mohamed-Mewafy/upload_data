import os
import time
import subprocess
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
import requests
import yt_dlp
import internetarchive as ia
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ACCESS_KEY = os.environ.get("IA_ACCESS_KEY")
SECRET_KEY = os.environ.get("IA_SECRET_KEY")

# اسم موقعك للعلامة المائية
WATERMARK_TEXT = "CimaSpace.site"

MAX_CONCURRENT_WORKERS = 2
log_lock = Lock()

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def log(msg):
    with log_lock:
        print(msg, flush=True)

def apply_watermark_with_ffmpeg(input_file, record_id):
    """تغطية العلامة المائية القديمة وإضافة اسم موقعك"""
    short_id = str(record_id)[:8]
    output_file = f"watermarked_{record_id}.mp4"
    log(f"🎨 [{short_id}] جاري معالجة العلامة المائية...")

    filter_complex = (
        f"delogo=x=10:y=10:w=200:h=60,"
        f"drawtext=text='{WATERMARK_TEXT}':x=15:y=25:fontsize=22:fontcolor=white:"
        f"box=1:boxcolor=black@0.6:boxborderw=5"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_file,
        "-vf", filter_complex,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "veryfast",
        "-c:a", "copy",
        output_file
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(output_file):
            log(f"✨ [{short_id}] تمت معالجة العلامة المائية بنجاح!")
            return output_file
    except Exception as e:
        log(f"⚠️ [{short_id}] تعذر تطبيق FFmpeg، سيتم استخدام الملف الأصلي: {e}")
    
    return input_file

def get_video_link_with_browser(embed_url, item_id):
    """استخراج رابط الفيديو المباشر عبر Playwright"""
    short_id = str(item_id)[:8]
    log(f"🌐 [{short_id}] تجربة الرابط: {embed_url}")
    extracted_url = None
    
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
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                extra_http_headers={"Referer": embed_url},
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()
            
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
    """تحميل الفيديو المحلي"""
    short_id = str(record_id)[:8]
    output_path = f"{record_id}.mp4"
    log(f"📥 [{short_id}] بدء التحميل...")
    
    ydl_opts = {
        'format': 'bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1][ext=mp4]/best[ext=mp4]/best',
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
                log(f"📦 [{short_id}] اكتمل التحميل المحلي ({file_size_mb:.1f} MB)")
                return output_path
            else:
                log(f"⚠️ [{short_id}] الملف غير صالح")
                if os.path.exists(output_path):
                    os.remove(output_path)
    except Exception as e:
        log(f"❌ [{short_id}] فشل التحميل المحلي: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
            
    return None

def verify_direct_url(url, retries=5, delay=3):
    """التأكد الفعلي من أن الفيديو مرفوع ويعمل برقم استجابة 200 OK قبل تحديث قاعدة البيانات"""
    for attempt in range(retries):
        try:
            res = requests.head(url, allow_redirects=True, timeout=10)
            if res.status_code == 200:
                content_length = int(res.headers.get('Content-Length', 0))
                if content_length > 1000000:  # التأكد أن الحجم أكبر من 1 ميجابايت
                    return True
        except Exception:
            pass
        time.sleep(delay)
    return False

def upload_to_archive(file_path, record_id, video_title="Movie"):
    """رفع الملف إلى Archive والتأكد المباشر من صحة الرابط"""
    short_id = str(record_id)[:8]
    identifier = f"cimaspace-item-{record_id}"
    target_filename = f"{identifier}.mp4"
    log(f"🚀 [{short_id}] جاري الرفع لـ Archive...")
    
    display_title = f"{video_title} - CimaSpace"

    metadata = {
        'mediatype': 'movies',
        'collection': 'opensource_movies',
        'title': display_title,
        'description': f'Watch {video_title} on CimaSpace'
    }

    try:
        r = ia.upload(
            identifier,
            files={target_filename: file_path},
            metadata=metadata,
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            retries=3,
            verbose=False
        )
        
        if r and r[0].status_code == 200:
            direct_mp4_url = f"https://archive.org/download/{identifier}/{target_filename}"
            
            # فحص تأكيدي إضافي للرابط
            log(f"🔍 [{short_id}] التحقق من جاهزية الرابط على سيرفرات الأرشيف...")
            if verify_direct_url(direct_mp4_url):
                log(f"✅ [{short_id}] تم الرفع والتحقق بنجاح! الرابط: {direct_mp4_url}")
                return direct_mp4_url
            else:
                log(f"⚠️ [{short_id}] الفيديو تم رفعه لكن الرابط غير جاهز بعد للتشغيل.")
                return None
        else:
            log(f"⚠️ [{short_id}] فشل استجابة الرفع لـ Archive")
            return None
    except Exception as e:
        log(f"❌ [{short_id}] خطأ أثناء الرفع: {e}")
        return None

def update_status(table_name, record_id, direct_mp4_url):
    """تحديث Supabase وتغيير is_uploaded إلى True حصراً عند النجاح الكامل"""
    short_id = str(record_id)[:8]
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "watch_url": direct_mp4_url
        }).eq("id", record_id).execute()
        log(f"✨ [{short_id}] تم التأكد وتحديث Supabase بنجاح!")
    except Exception as e:
        log(f"❌ [{short_id}] خطأ في التحديث: {e}")

def process_single_item(item, table_name, url_column, title_column):
    record_id = item.get("id")
    if not record_id:
        return False
        
    short_id = str(record_id)[:8]
    title = item.get(title_column) or "CimaSpace Video"
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
                
                processed_file = apply_watermark_with_ffmpeg(local_file, record_id)
                direct_mp4_url = upload_to_archive(processed_file, record_id, video_title=title)
                
                # حذف الملفات المؤقتة
                if os.path.exists(local_file):
                    os.remove(local_file)
                if processed_file != local_file and os.path.exists(processed_file):
                    os.remove(processed_file)

                # التحديث يحدث فقط إذا أرجع upload_to_archive رابطاً مؤكداً ومفحوصاً
                if direct_mp4_url:
                    update_status(table_name, record_id, direct_mp4_url)
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
    process_table_parallel("movies_cima", "watch_url", "title", limit=10)
    process_table_parallel("arabic_movies", "watch_url", "title", limit=10)
    process_table_parallel("tv_series", "watch_url", "title", limit=10)
    process_table_parallel("episodes_cima", "watch_url", "title", limit=10)
    
    log("\n🏁 انتهت كل العمليات!")

if __name__ == "__main__":
    main()
