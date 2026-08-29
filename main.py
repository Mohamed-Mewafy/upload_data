import os
import time
import yt_dlp
import internetarchive as ia
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ACCESS_KEY = os.environ.get("IA_ACCESS_KEY")
SECRET_KEY = os.environ.get("IA_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}", flush=True)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_video_link_with_browser(embed_url):
    print(f"🌐 فتح المتصفح الوهمي لفحص الرابط: {embed_url}", flush=True)
    extracted_url = None
    
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
                    print(f"🎯 تم صيد الرابط المباشر بنجاح: {url}", flush=True)

        page.on("request", lambda req: check_url(req.url))
        page.on("response", lambda res: check_url(res.url))
        
        try:
            page.goto(embed_url, timeout=45000, wait_until="domcontentloaded")
            for i in range(4):
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
                
                time.sleep(5)
                if extracted_url:
                    break
        except Exception as e:
            print(f"⚠️ خطأ أثناء تصفح الصفحة: {e}", flush=True)
            
        browser.close()
    return extracted_url

def download_video_temporarily(video_url, record_id, output_dir="."):
    output_path = os.path.join(output_dir, f"{record_id}.mp4")
    print(f"📥 جاري تحميل وتجميع الفيديو باستخدام (yt-dlp)...", flush=True)
    
    ydl_opts = {
        'format': 'best',
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': True,
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
            print(f"📦 حجم الملف المنزل: {file_size_mb:.2f} MB", flush=True)
            
            if file_size_mb < 2:
                print(f"❌ الملف صغير جداً ({file_size_mb:.2f} MB)، غالباً الرابط تالف.", flush=True)
                os.path.exists(output_path) and os.remove(output_path)
                return None
                
            print("✅ تم التحميل والتجميع بنجاح تام!", flush=True)
            return output_path
    except Exception as e:
        print(f"❌ خطأ أثناء التحميل بـ yt-dlp: {e}", flush=True)
        
    if os.path.exists(output_path):
        os.remove(output_path)
    return None

def upload_to_archive(file_path, record_id):
    identifier = f"cimaspace-item-{record_id}"
    file_name = f"{record_id}.mp4"
    print(f"📤 جاري رفع الملف بالمعرف العشوائي [{identifier}] إلى Archive.org...", flush=True)
    
    metadata = {
        'mediatype': 'movies',
        'collection': 'movies',
        'title': f"Media Item {record_id}",
        'description': 'Encrypted media storage.'
    }
    
    try:
        r = ia.upload(
            identifier,
            files=[file_path],
            metadata=metadata,
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY
        )
        
        if r and r[0].status_code == 200:
            archive_download_url = f"https://archive.org/download/{identifier}/{file_name}"
            print(f"✅ تم الرفع بنجاح! الرابط: {archive_download_url}", flush=True)
            return archive_download_url
        else:
            print(f"⚠️ فشل الرفع للأرشيف.", flush=True)
            return None
    except Exception as e:
        print(f"❌ خطأ أثناء الرفع: {e}", flush=True)
        return None

def update_status(table_name, record_id, archive_url):
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "watch_url": archive_url
        }).eq("id", record_id).execute()
        print(f"🔄 تم تحديث حالة الرفع والرابط في جدول [{table_name}] بنجاح.", flush=True)
    except Exception as e:
        print(f"❌ خطأ أثناء التحديث: {e}", flush=True)

def process_table(table_name, url_column, title_column="title"):
    print(f"\n========================================", flush=True)
    print(f"📂 فحص الجدول: {table_name}", flush=True)
    print(f"========================================", flush=True)
    try:
        response = supabase.table(table_name).select(f"id, {title_column}, {url_column}").eq("is_uploaded", False).limit(5).execute()
        items = response.data
    except Exception as e:
        print(f"❌ خطأ في جلب البيانات من {table_name}: {e}", flush=True)
        return

    if not items:
        print(f"🎉 لا توجد عناصر جديدة في جدول {table_name}.", flush=True)
        return

    for index, item in enumerate(items, 1):
        record_id = item.get("id")
        title = item.get(title_column) or "Unamed Video"
        watch_url = item.get(url_column)
        
        if not watch_url:
            continue
            
        print(f"\n----------------------------------------", flush=True)
        print(f"[{index}] معالجة العنصر: {title} (ID: {record_id})", flush=True)
        print(f"----------------------------------------", flush=True)
        
        direct_url = get_video_link_with_browser(watch_url)
        if direct_url:
            local_file = download_video_temporarily(direct_url, record_id)
            if local_file and os.path.exists(local_file):
                archive_url = upload_to_archive(local_file, record_id)
                if archive_url:
                    update_status(table_name, record_id, archive_url)
                
                if os.path.exists(local_file):
                    os.remove(local_file)
        else:
            print("❌ فشل استخراج الرابط المباشر بواسطة المتصفح.", flush=True)

def main():
    process_table("movies_cima", "watch_url", "title")
    process_table("arabic_movies", "watch_url", "title")
    process_table("tv_series", "watch_url", "title")
    process_table("episodes_cima", "watch_url", "title")
    
    print("\n🏁 انتهت كل المهام بنجاح!", flush=True)

if __name__ == "__main__":
    main()
