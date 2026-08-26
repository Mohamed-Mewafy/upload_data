import os
import time
import requests
import internetarchive as ia
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# قراءة متغيرات البيئة بأمان من GitHub Secrets
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ACCESS_KEY = os.environ.get("IA_ACCESS_KEY")
SECRET_KEY = os.environ.get("IA_SECRET_KEY")

# التحقق من صحة الرابط قبل الاتصال
if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}", flush=True)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_video_link_with_browser(embed_url):
    print(f"🌐 فتح المتصفح الوهمي لفحص الرابط: {embed_url}", flush=True)
    extracted_url = None
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            extra_http_headers={"Referer": "https://cimaspace.site/"}
        )
        page = context.new_page()
        
        def intercept_response(response):
            nonlocal extracted_url
            url = response.url
            if any(ext in url for ext in ['.m3u8', '.mp4', '.ts']) and 'chunk' not in url:
                if not extracted_url:
                    extracted_url = url
                    print(f"🎯 تم صيد الرابط المباشر: {url}", flush=True)

        page.on("response", intercept_response)
        
        try:
            page.goto(embed_url, timeout=35000)
            for selector in ["video", ".play-btn", ".jw-display-icon-container", "#vplayer", ".vjs-big-play-button"]:
                try:
                    if page.locator(selector).count() > 0:
                        page.click(selector, timeout=2000)
                except:
                    pass
            time.sleep(6)
        except Exception as e:
            print(f"⚠️ خطأ أثناء تصفح الصفحة: {e}", flush=True)
            
        browser.close()
    return extracted_url

def download_video_temporarily(video_url, output_path="temp_video.mp4"):
    print(f"📥 جاري تحميل الفيديو مؤقتاً لمعالجته...", flush=True)
    try:
        response = requests.get(video_url, stream=True, timeout=60, verify=False)
        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
            print("✅ تم التحميل المؤقت بنجاح.", flush=True)
            return output_path
    except Exception as e:
        print(f"❌ خطأ أثناء التحميل: {e}", flush=True)
    return None

def upload_to_archive(file_path, record_id):
    identifier = f"cimaspace-item-{record_id}"
    
    print(f"📤 جاري رفع الفيلم بالمعرف السري [{identifier}] إلى Archive.org...", flush=True)
    
    metadata = {
        'mediatype': 'movies',
        'title': f"Protected Media {record_id}",
        'description': 'Encrypted media storage for Cimaspace platform.'
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
            archive_download_url = f"https://archive.org/download/{identifier}/temp_video.mp4"
            print(f"✅ تم الرفع بنجاح! الرابط المشفر: {archive_download_url}", flush=True)
            return archive_download_url
        else:
            print(f"⚠️ فشل الرفع للأرشيف.", flush=True)
            return None
    except Exception as e:
        print(f"❌ خطأ أثناء الرفع: {e}", flush=True)
        return None

def update_status(table_name, record_id):
    try:
        supabase.table(table_name).update({
            "is_uploaded": True
        }).eq("id", record_id).execute()
        print(f"🔄 تم تحديث حالة الرفع في جدول [{table_name}] بنجاح.", flush=True)
    except Exception as e:
        print(f"❌ خطأ أثناء التحديث: {e}", flush=True)

def process_table(table_name, url_column, title_column="title"):
    print(f"\n========================================", flush=True)
    print(f"📂 فحص الجدول: {table_name}", flush=True)
    print(f"========================================", flush=True)
    try:
        response = supabase.table(table_name).select(f"id, {title_column}, {url_column}").eq("is_uploaded", False).execute()
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
            local_file = download_video_temporarily(direct_url)
            if local_file and os.path.exists(local_file):
                archive_url = upload_to_archive(local_file, record_id)
                if archive_url:
                    update_status(table_name, record_id)
                
                if os.path.exists(local_file):
                    os.remove(local_file)
        else:
            print("❌ فشل استخراج الرابط المباشر.", flush=True)

def main():
    process_table("movies_cima", "watch_url", "title")
    process_table("arabic_movies", "watch_url", "title")
    process_table("tv_series", "watch_url", "title")
    process_table("episodes_cima", "watch_url", "title")
    
    print("\n🏁 انتهت كل المهام بنجاح!", flush=True)

if __name__ == "__main__":
    main()
