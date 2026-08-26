import os
import time
import requests
import internetarchive as ia
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# قراءة البيانات بأمان من بيئة جيت هب
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ACCESS_KEY = os.environ.get("IA_ACCESS_KEY")
SECRET_KEY = os.environ.get("IA_SECRET_KEY")


def get_video_link_with_browser(embed_url):
    print(f"🌐 فتح المتصفح الوهمي لفحص الرابط: {embed_url}")
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
                    print(f"🎯 تم صيد الرابط المباشر: {url}")

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
            print(f"⚠️ خطأ أثناء تصفح الصفحة: {e}")
            
        browser.close()
    return extracted_url

def download_video_temporarily(video_url, output_path="temp_video.mp4"):
    print(f"📥 جاري تحميل الفيديو مؤقتاً لمعالجته...")
    try:
        response = requests.get(video_url, stream=True, timeout=60, verify=False)
        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
            print("✅ تم التحميل المؤقت بنجاح.")
            return output_path
    except Exception as e:
        print(f"❌ خطأ أثناء التحميل: {e}")
    return None

def upload_to_archive(file_path, media_title, movie_uid):
    # استخدام الـ uid الخاص بالفيلم كمعرف أساسي للأرشيف ليكون مشفراً ومخفياً
    identifier = f"cimaspace-item-{movie_uid}"
    
    print(f"📤 جاري رفع الفيلم بالمعرف السري [{identifier}] إلى Archive.org...")
    
    metadata = {
        'mediatype': 'movies',
        'title': f"Protected Media {movie_uid}", # إخفاء العنوان الحقيقي في الأرشيف أيضاً
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
            # استخراج اسم الملف الفعلي المرفوع
            file_name = os.path.basename(file_path)
            archive_download_url = f"https://archive.org/download/{identifier}/{file_name}"
            print(f"✅ تم الرفع بنجاح! الرابط المشفر: {archive_download_url}")
            return archive_download_url
        else:
            print(f"⚠️ فشل الرفع للأرشيف.")
            return None
    except Exception as e:
        print(f"❌ خطأ أثناء الرفع: {e}")
        return None

def update_status(table_name, record_id, archive_url):
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "archive_url": archive_url
        }).eq("id", record_id).execute()
        print(f"🔄 تم تحديث السجل وحفظ الرابط في جدول [{table_name}] بنجاح.")
    except Exception as e:
        try:
            supabase.table(table_name).update({"is_uploaded": True}).eq("id", record_id).execute()
            print(f"🔄 تم تحديث حالة is_uploaded في جدول [{table_name}] بنجاح.")
        except Exception as inner_e:
            print(f"❌ خطأ أثناء التحديث: {inner_e}")

def process_table(table_name, url_column, title_column="title", uid_column="uid"):
    print(f"\n========================================")
    print(f"📂 فحص الجدول: {table_name}")
    print(f"========================================")
    try:
        # جلب الـ id والـ title والـ watch_url ومعرف الـ uid الخاص بالفيلم
        response = supabase.table(table_name).select(f"id, {title_column}, {url_column}, {uid_column}").eq("is_uploaded", False).execute()
        items = response.data
    except Exception as e:
        print(f"❌ خطأ في جلب البيانات من {table_name}: {e}")
        return

    if not items:
        print(f"🎉 لا توجد عناصر جديدة في جدول {table_name}.")
        return

    for index, item in enumerate(items, 1):
        record_id = item.get("id")
        title = item.get(title_column) or "Unamed Video"
        watch_url = item.get(url_column)
        movie_uid = item.get(uid_column) or str(record_id) # استخدام الـ uid أو الـ id كبديل احتياطي
        
        if not watch_url:
            continue
            
        print(f"\n----------------------------------------")
        print(f"[{index}] معالجة الفيلم: {title} (UID: {movie_uid})")
        print(f"----------------------------------------")
        
        direct_url = get_video_link_with_browser(watch_url)
        if direct_url:
            local_file = download_video_temporarily(direct_url)
            if local_file and os.path.exists(local_file):
                archive_url = upload_to_archive(local_file, title, movie_uid)
                if archive_url:
                    update_status(table_name, record_id, archive_url)
                
                os.remove(local_file)
        else:
            print("❌ فشل استخراج الرابط المباشر.")

def main():
    process_table("movies_cima", "watch_url", "title", "uid")
    process_table("arabic_movies", "watch_url", "title", "uid")
    process_table("tv_series", "watch_url", "title", "uid")
    process_table("episodes_cima", "watch_url", "title", "uid")
    
    print("\n🏁 انتهت كل المهام بنجاح!")

if __name__ == "__main__":
    main()
