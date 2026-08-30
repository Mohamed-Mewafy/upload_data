import os
import sys
import asyncio
import subprocess
from supabase import create_client, Client
from playwright.async_api import async_playwright
from internetarchive import upload

# 1. إعداد متغيرات البيئة ورابط Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
IA_ACCESS_KEY = os.environ.get("IA_ACCESS_KEY")
IA_SECRET_KEY = os.environ.get("IA_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("❌ خطأ: يرجى التأكد من تعيين SUPABASE_URL و SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 2. استخراج رابط MP4 المباشر من سيرفرات الإمبد باستخدام Playwright
async def get_direct_video_url(embed_url):
    direct_url = None
    async with async_playwright() as p:
        # تشغيل المتصفح بوضع الخفاء مع إعدادات تخفيف الحمل
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        context = await browser.new_context()
        page = await context.new_page()

        # حجب الصور والملفات الثقيلة لتسريع عملية الفحص
        async def block_resources(route):
            if route.request.resource_type in ["image", "stylesheet", "font"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_resources)

        # التقاط طلبات الشبكة للبحث عن امتداد الفيديو المباشر
        def handle_request(request):
            nonlocal direct_url
            url = request.url
            if (".mp4" in url or ".m3u8" in url) and not direct_url:
                if "googlevideo" not in url and "analytics" not in url:
                    direct_url = url

        page.on("request", handle_request)

        try:
            await page.goto(embed_url, wait_until="domcontentloaded", timeout=20000)
            for _ in range(12):  # الانتظار لمدة تصل إلى 6 ثوانٍ للتقاط الفيديو
                if direct_url:
                    break
                await asyncio.sleep(0.5)
        except Exception as e:
            pass
        finally:
            await browser.close()

    return direct_url

# 3. تحميل الفيديو إلى السيرفر المحتوي على السكربت باستخدام yt-dlp
def download_video(video_url, output_path):
    print(f"⬇️ جاري تنزيل الفيديو إلى السيرفر المحلي...")
    command = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "-o", output_path,
        "--no-playlist",
        video_url
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0

# 4. رفع الملف إلى Internet Archive وتوليد الرابط المباشر
def upload_to_archive(file_path, identifier, title):
    print(f"🚀 جاري الرفع إلى Internet Archive...")
    file_name = os.path.basename(file_path)
    
    # الرفع عبر مكتبة internetarchive
    status = upload(
        identifier,
        files=[file_path],
        metadata={'title': title, 'mediatype': 'movies'},
        access_key=IA_ACCESS_KEY,
        secret_key=IA_SECRET_KEY
    )
    
    if status[0].status_code == 200:
        # تركيب الرابط المباشر للملف
        direct_archive_url = f"https://archive.org/download/{identifier}/{file_name}"
        return direct_archive_url
    return None

# 5. الدالة الرئيسية لمعالجة كل فيلم
async def process_movie(movie):
    movie_id = movie.get("id")
    title = movie.get("title", f"movie_{movie_id}")
    embed_links = movie.get("embed_links", [])

    print(f"\n🎬 [ID: {movie_id}] بدء معالجة الفيلم: {title}")

    if not embed_links:
        print(f"⚠️ لا توجد روابط مفرغة للفيلم: {title}")
        return

    direct_source_url = None
    for index, embed_url in enumerate(embed_links, 1):
        print(f"🔗 تجربة الرابط ({index}/{len(embed_links)}): {embed_url}")
        direct_source_url = await get_direct_video_url(embed_url)
        if direct_source_url:
            print(f"✅ تم العثور على مصدر الفيديو المباشر!")
            break

    if not direct_source_url:
        print(f"❌ فشل استخراج رابط الفيديو المباشر لجميع السيرفرات المتاحة.")
        return

    # مسار التخزين المؤقت للفيلم
    clean_title = "".join(c for c in title if c.isalnum() or c in (' ', '_')).rstrip()
    filename = f"{clean_title}.mp4"
    temp_path = os.path.join("/tmp", filename)
    identifier = f"cima_space_{movie_id}"

    try:
        # أ) التحميل
        download_success = download_video(direct_source_url, temp_path)
        if not download_success or not os.path.exists(temp_path):
            print(f"❌ فشل تحميل ملف الفيديو.")
            return

        # ب) الرفع للحصول على رابط .mp4
        direct_archive_mp4 = upload_to_archive(temp_path, identifier, title)

        if direct_archive_mp4:
            print(f"🎉 تم الرفع بنجاح! الرابط المباشر: {direct_archive_mp4}")

            # ج) تحديث قاعدة بيانات Supabase بالرابط المباشر
            supabase.table("movies_cima").update({
                "stream_url": direct_archive_mp4,
                "status": "completed"
            }).eq("id", movie_id).execute()
            print(f"💾 تم تحديث Supabase بنجاح.")
        else:
            print(f"❌ فشلت عملية الرفع لـ Internet Archive.")

    finally:
        # د) حذف الملف المؤقت بعد الانتهاء لتوفير المساحة
        if os.path.exists(temp_path):
            os.remove(temp_path)

# 6. التشغيل التنفيذي للمشروع
async def main():
    print("==========================================")
    print("📂 جلب البيانات المعلقة من Supabase...")
    print("==========================================")

    # جلب الأفلام التي لم تُعالج بعد (status is null أو pending)
    response = supabase.table("movies_cima").select("*").is_("stream_url", "null").limit(100).execute()
    movies = response.data

    if not movies:
        print("✨ لا توجد أفلام معلقة بانتظار المعالجة.")
        return

    for movie in movies:
        await process_movie(movie)

if __name__ == "__main__":
    asyncio.run(main())
