import os
import time
import subprocess
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
import requests
import yt_dlp
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
VK_ACCESS_TOKEN = os.environ.get("VK_ACCESS_TOKEN")

WATERMARK_TEXT = "CimaSpace.site"
MAX_CONCURRENT_WORKERS = 2
log_lock = Lock()
processed_ids_lock = Lock()
in_memory_locked_ids = set()

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    raise ValueError(f"❌ خطأ: رابط Supabase غير صحيح أو فارغ: {SUPABASE_URL}")

if not VK_ACCESS_TOKEN:
    raise ValueError("❌ خطأ: رمز الوصول VK_ACCESS_TOKEN غير معرف في البيئة.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def log(msg):
    with log_lock:
        print(msg, flush=True)

def try_lock_record(table_name, record_id):
    """حجز العنصر بالذاكرة وفي Supabase لمنع التكرار نهائياً"""
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

def apply_watermark_with_ffmpeg(input_file, record_id):
    """تغطية العلامة المائية القديمة وإضافة اسم موقعك باستعمال FFmpeg"""
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
    except FileNotFoundError:
        log(f"⚠️ [{short_id}] برنامج FFmpeg غير مثبت، سيتم تجاوز معالجة العلامة المائية.")
    except Exception as e:
        log(f"⚠️ [{short_id}] تعذر تطبيق FFmpeg: {e}")
    
    return input_file

def get_video_link_with_browser(embed_url, item_id):
    """استخراج رابط الفيديو المباشر عبر Playwright"""
    short_id = str(item_id)[:8]
    log(f"🌐 [{short_id}] تجربة الرابط: {embed_url}")
    extracted_url = None
    
    if any(domain in embed_url.lower() for domain in ["archive.org", "vk.com", "vk.ru"]):
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
                if any(ext in url_lower for ext in ['.m3u8', '.mp4', 'video/mp4']) and not any(ign in url_lower for ign in ['archive.org', 'vk.com', 'vk.ru', 'chunk', 'ads', 'seg', 'analytics', 'googlevideo']):
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
    """تحميل الفيديو المحلي مع تفادي استجابة 403"""
    short_id = str(record_id)[:8]
    output_path = f"{record_id}.mp4"
    log(f"📥 [{short_id}] بدء التحميل...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": embed_url if embed_url else "https://google.com/",
    }

    try:
        head_res = requests.head(video_url, headers=headers, timeout=5, allow_redirects=True)
        if head_res.status_code == 403:
            log(f"⚠️ [{short_id}] تم رفض الوصول للرابط (HTTP 403)، الانتقال للسيرفر التالي...")
            return None
    except Exception:
        pass

    if video_url.endswith(".mp4"):
        try:
            with requests.get(video_url, headers=headers, stream=True, timeout=30) as r:
                if r.status_code == 200:
                    with open(output_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
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
        'retries': 15,
        'fragment_retries': 20,
        'skip_unavailable_fragments': True,
        'concurrent_fragment_downloads': 5,
        'http_headers': headers
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

def upload_to_vk(file_path, record_id):
    """رفع الفيديو إلى VK وتسميته بـ UUID الفيلم حصراً"""
    short_id = str(record_id)[:8]
    display_title = str(record_id)
    description = f"UUID: {record_id}"
    
    log(f"🚀 [{short_id}] الحصول على سيرفر الرفع من VK...")
    
    save_url = "https://api.vk.com/method/video.save"
    params = {
        "name": display_title,
        "description": description,
        "is_private": 0,
        "wallpost": 0,
        "access_token": VK_ACCESS_TOKEN,
        "v": "5.131"
    }

    try:
        res = requests.post(save_url, data=params, timeout=15).json()
        if "error" in res:
            log(f"❌ [{short_id}] خطأ VK API: {res['error'].get('error_msg')}")
            return None

        upload_url = res["response"]["upload_url"]
        owner_id = res["response"]["owner_id"]
        video_id = res["response"]["video_id"]
        access_key = res["response"].get("access_key", "")

        log(f"⬆️ [{short_id}] جاري إرسال الملف إلى سيرفرات VK...")
        
        with open(file_path, "rb") as f:
            upload_res = requests.post(
                upload_url,
                files={"video_file": f},
                timeout=1800
            ).json()

        if upload_res.get("video_id") or upload_res.get("result"):
            hash_param = f"&hash={access_key}" if access_key else ""
            embed_player_url = f"https://vk.ru/video_ext.php?oid={owner_id}&id={video_id}{hash_param}"
            log(f"✅ [{short_id}] تم الرفع إلى VK بنجاح! رابط المشغل: {embed_player_url}")
            return embed_player_url
        else:
            log(f"⚠️ [{short_id}] استجابة سيرفر الرفع غير مكتملة: {upload_res}")
            return None

    except Exception as e:
        log(f"❌ [{short_id}] خطأ أثناء الرفع إلى VK: {e}")
        return None

def update_status(table_name, record_id, watch_url):
    """تحديث حالة الفيلم ورابط المشاهدة المباشر في Supabase"""
    short_id = str(record_id)[:8]
    try:
        supabase.table(table_name).update({
            "is_uploaded": True,
            "is_processing": False,
            "watch_url": watch_url
        }).eq("id", record_id).execute()
        log(f"✨ [{short_id}] تم التحديث وقفل السجل في Supabase بنجاح!")
    except Exception:
        try:
            supabase.table(table_name).update({
                "is_uploaded": True,
                "watch_url": watch_url
            }).eq("id", record_id).execute()
            log(f"✨ [{short_id}] تم تحديث السجل بنجاح!")
        except Exception as e:
            log(f"❌ [{short_id}] خطأ في التحديث: {e}")

def process_single_item(item, table_name, url_column, title_column):
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

    log(f"\n🎬 [{short_id}] بدء المعالجة: {title}")
    
    for link_index, current_url in enumerate(urls_to_try, 1):
        log(f"🔗 [{short_id}] تجربة الرابط ({link_index}/{len(urls_to_try)})...")
        
        direct_url, embed_src = get_video_link_with_browser(current_url, record_id)
        if direct_url:
            local_file = download_video_temporarily(direct_url, embed_src, record_id)
            if local_file and os.path.exists(local_file):
                
                processed_file = apply_watermark_with_ffmpeg(local_file, record_id)
                vk_watch_url = upload_to_vk(processed_file, record_id)
                
                if os.path.exists(local_file):
                    os.remove(local_file)
                if processed_file != local_file and os.path.exists(processed_file):
                    os.remove(processed_file)

                if vk_watch_url:
                    update_status(table_name, record_id, vk_watch_url)
                    log(f"🎉 [{short_id}] اكتملت العملية بنجاح للفيلم: {title}\n")
                    return True
                
        log(f"🔄 [{short_id}] الانتقال للرابط التالي...")
        
    log(f"❌ [{short_id}] فشلت جميع الروابط المتاحة للفيلم: {title}\n")
    unlock_record_on_failure(table_name, record_id)
    return False

def process_table_parallel(table_name, url_column, title_column="title", limit=10):
    log(f"\n==========================================")
    log(f"📂 فحص الجدول: {table_name}")
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

    # استبعاد العناصر المعالجة حالياً بالذاكرة
    valid_items = [item for item in items if item.get("id") and item.get("id") not in in_memory_locked_ids]

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        futures = [
            executor.submit(process_single_item, item, table_name, url_column, title_column) 
            for item in valid_items
        ]
        
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"❌ خطأ غير متوقع في المهمة: {e}")

def main():
    process_table_parallel("movies_cima", "watch_url", "title", limit=1000)
    process_table_parallel("arabic_movies", "watch_url", "title", limit=1000)
    process_table_parallel("tv_series", "watch_url", "title", limit=1000)
    process_table_parallel("episodes_cima", "watch_url", "title", limit=1000)
    
    log("\n🏁 انتهت كل العمليات!")

if __name__ == "__main__":
    main()
