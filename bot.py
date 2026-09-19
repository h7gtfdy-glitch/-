"""
بوت تليجرام لإزالة خلفية الفيديو (مخصص للأنمي عبر نموذج isnet-anime)
------------------------------------------------------------------
يستقبل فيديو من المستخدم، يسأله عن الخلفية الجديدة بأزرار، يعالج
الفريمات بمكتبة rembg، ثم يعيد تجميع الفيديو مع الصوت الأصلي عبر ffmpeg.

قبل التشغيل لازم:
  - متغير بيئة BOT_TOKEN (توكن البوت من @BotFather)
  - ffmpeg مثبت على السيرفر (متوفر تلقائياً لو استخدمت Dockerfile المرفق)

القيود (مهم تعرفها):
  - تليجرام يسمح للبوتات بتحميل ملفات المستخدم بحد أقصى 20 ميجا فقط.
  - الرامات محدودة على الاستضافة المجانية (512 ميجا)، فالكود يحدد مدة
    ودقة الفيديو المسموحة عشان ما تفشل المعالجة.
  - معالجة كل فريم بالذكاء الاصطناعي أبطأ من المتصفح، فالفيديوهات
    الطويلة بتاخذ وقت أطول.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import cv2
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# الإعدادات (تقدر تغيّرها بمتغيرات بيئة بدون لمس الكود)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))
REMBG_MODEL = os.environ.get("REMBG_MODEL", "isnet-anime")  # مخصص للأنمي
MAX_DURATION_SEC = float(os.environ.get("MAX_DURATION_SEC", "25"))
MAX_WIDTH = int(os.environ.get("MAX_WIDTH", "640"))
TARGET_FPS = float(os.environ.get("TARGET_FPS", "15"))  # لتخفيف الحمل على السيرفر المجاني

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bg-bot")

BACKGROUNDS = {
    "green": ("أخضر (لتطبيق كروما لاحقاً)", (0, 177, 64)),
    "blue": ("أزرق", (0, 71, 255)),
    "black": ("أسود", (0, 0, 0)),
    "white": ("أبيض", (255, 255, 255)),
    "transparent": ("شفافة (WebM تجريبي)", None),
}

# ffmpeg: نفضّل النسخة المثبتة بالنظام، ولو ما لقيناها نستخدم نسخة imageio-ffmpeg المرفقة كـ fallback
FFMPEG_BIN = shutil.which("ffmpeg")
if not FFMPEG_BIN:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

# ---------------------------------------------------------------------------
# نموذج rembg يتحمّل مرة وحدة ويُعاد استخدامه (تحميله أول مرة ياخذ وقت لأنه
# ينزّل ملف النموذج من الإنترنت، حوالي 170-200 ميجا)
# ---------------------------------------------------------------------------
_session = None
_session_lock = threading.Lock()


def get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                from rembg import new_session
                log.info("Loading rembg model '%s' (first time may take a while)...", REMBG_MODEL)
                _session = new_session(REMBG_MODEL)
                log.info("Model loaded.")
    return _session


def run(cmd: list[str]):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(f"فشل الأمر: {' '.join(cmd)}\n{result.stderr.decode(errors='ignore')[-800:]}")
    return result


def extract_audio(src: Path, out: Path) -> bool:
    """يرجع True لو فيه صوت وتم استخراجه بنجاح."""
    try:
        run([FFMPEG_BIN, "-y", "-i", str(src), "-vn", "-acodec", "aac", str(out)])
        return out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def process_video(src_path: Path, bg_choice: str, workdir: Path, progress_cb=None) -> Path:
    """يقص الخلفية من كل فريم ويركّب الخلفية الجديدة، ويرجع مسار الفيديو الناتج."""
    from rembg import remove

    cap = cv2.VideoCapture(str(src_path))
    if not cap.isOpened():
        raise RuntimeError("تعذر قراءة ملف الفيديو.")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    fps = min(src_fps, TARGET_FPS) if TARGET_FPS > 0 else src_fps
    scale = min(1.0, MAX_WIDTH / max(src_w, 1))
    out_w, out_h = max(2, int(src_w * scale) // 2 * 2), max(2, int(src_h * scale) // 2 * 2)

    frame_interval = max(1.0, src_fps / fps)
    session = get_session()
    solid_rgba = None
    if bg_choice != "transparent":
        color = BACKGROUNDS[bg_choice][1]
        solid_rgba = Image.new("RGBA", (out_w, out_h), color + (255,))

    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)

    idx = 0
    saved = 0
    next_take = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx >= next_take:
            next_take += frame_interval
            img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(img).resize((out_w, out_h))
            cut = remove(pil_img, session=session)  # RGBA بخلفية شفافة
            if solid_rgba is not None:
                composed = Image.alpha_composite(solid_rgba, cut)
                composed = composed.convert("RGB")
                composed.save(frames_dir / f"f_{saved:06d}.jpg", quality=92)
            else:
                cut.save(frames_dir / f"f_{saved:06d}.png")
            saved += 1
            if progress_cb and total_frames:
                progress_cb(min(99, int(idx / total_frames * 100)))
        idx += 1
    cap.release()

    if saved == 0:
        raise RuntimeError("ما قدرنا نستخرج أي فريم من الفيديو.")

    # الصوت الأصلي (إن وجد)
    audio_path = workdir / "audio.m4a"
    has_audio = extract_audio(src_path, audio_path)

    pattern = str(frames_dir / "f_%06d.jpg") if solid_rgba is not None else str(frames_dir / "f_%06d.png")
    out_path = workdir / ("output.mp4" if solid_rgba is not None else "output.webm")

    if solid_rgba is not None:
        cmd = [FFMPEG_BIN, "-y", "-framerate", str(fps), "-i", pattern]
        if has_audio:
            cmd += ["-i", str(audio_path)]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23"]
        if has_audio:
            cmd += ["-c:a", "aac", "-shortest"]
        cmd += [str(out_path)]
    else:
        # فيديو شفاف تجريبي (WebM + ألفا) — تطبيقات كثيرة ما تعرضه صح
        cmd = [FFMPEG_BIN, "-y", "-framerate", str(fps), "-i", pattern,
               "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(out_path)]

    run(cmd)
    return out_path


# ---------------------------------------------------------------------------
# طابور معالجة: فيديو وحدة بوقت واحد عشان ما يتعب السيرفر المجاني
# ---------------------------------------------------------------------------
job_queue: asyncio.Queue = asyncio.Queue()


async def worker(app: Application):
    while True:
        job = await job_queue.get()
        try:
            await handle_job(app, **job)
        except Exception as e:  # noqa: BLE001
            log.exception("Job failed")
            try:
                await app.bot.edit_message_text(
                    chat_id=job["chat_id"], message_id=job["status_msg_id"],
                    text=f"صار خطأ أثناء المعالجة: {e}",
                )
            except Exception:
                pass
        finally:
            job_queue.task_done()


async def handle_job(app: Application, chat_id: int, status_msg_id: int, file_id: str, bg_choice: str):
    bot = app.bot
    label = BACKGROUNDS[bg_choice][0]
    await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=f"جاري تحميل الفيديو…")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        src_path = workdir / "input.mp4"
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(str(src_path))

        # تأكد من مدة الفيديو
        cap = cv2.VideoCapture(str(src_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 24
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = frame_count / fps if fps else 0
        cap.release()
        if duration > MAX_DURATION_SEC:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg_id,
                text=f"الفيديو أطول من الحد المسموح ({MAX_DURATION_SEC:.0f} ثانية). جرّب مقطع أقصر.",
            )
            return

        await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id,
                                     text=f"جاري القص وتركيب الخلفية ({label})… 0%")

        loop = asyncio.get_event_loop()
        last_reported = {"v": -1}

        def progress_cb(pct):
            if pct - last_reported["v"] >= 8:
                last_reported["v"] = pct
                asyncio.run_coroutine_threadsafe(
                    bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id,
                                           text=f"جاري القص وتركيب الخلفية ({label})… {pct}%"),
                    loop,
                )

        out_path = await loop.run_in_executor(
            None, process_video, src_path, bg_choice, workdir, progress_cb
        )

        await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text="جاري رفع النتيجة…")
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        if bg_choice == "transparent":
            await bot.send_document(
                chat_id=chat_id, document=out_path.open("rb"),
                filename="cutout-transparent.webm",
                caption="فيديو شفاف تجريبي (WebM+alpha). ملاحظة: أغلب التطبيقات ما تعرض الشفافية بهذي الصيغة صح.",
            )
        else:
            await bot.send_video(chat_id=chat_id, video=out_path.open("rb"), supports_streaming=True,
                                  caption=f"تم! الخلفية: {label}")

        await bot.delete_message(chat_id=chat_id, message_id=status_msg_id)


# ---------------------------------------------------------------------------
# أوامر وأحداث تليجرام
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أرسل لي فيديو (يفضل أقل من "
        f"{MAX_DURATION_SEC:.0f} ثانية وحجم أقل من 20 ميجا) وبقصّ الخلفية "
        "تلقائياً وأخيّرك بأي خلفية جديدة تبيها."
    )


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    video = msg.video or msg.document
    if video is None:
        return
    if getattr(video, "file_size", None) and video.file_size > 20 * 1024 * 1024:
        await msg.reply_text("الفيديو أكبر من 20 ميجا، وهذا أقصى حجم يقدر البوت يحمّله من تليجرام. جرّب مقطع أصغر.")
        return

    context.chat_data["pending_file_id"] = video.file_id
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"bg:{key}")]
        for key, (label, _) in BACKGROUNDS.items()
    ]
    await msg.reply_text("اختر الخلفية الجديدة:", reply_markup=InlineKeyboardMarkup(buttons))


async def on_bg_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bg_choice = query.data.split(":", 1)[1]
    file_id = context.chat_data.get("pending_file_id")
    if not file_id:
        await query.edit_message_text("ما لقيت فيديو مرتبط بهالطلب. أرسل الفيديو من جديد.")
        return

    await query.edit_message_text("تمت الإضافة للطابور، جاري البدء…")
    await job_queue.put({
        "chat_id": query.message.chat_id,
        "status_msg_id": query.message.message_id,
        "file_id": file_id,
        "bg_choice": bg_choice,
    })


# ---------------------------------------------------------------------------
# سيرفر HTTP صغير فقط عشان يرضي فحص الصحة بمنصات الاستضافة (Render وغيرها)
# ---------------------------------------------------------------------------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):  # كتم اللوق الافتراضي المزعج
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


async def post_init(app: Application):
    app.create_task(worker(app))


def main():
    if not BOT_TOKEN:
        raise SystemExit("لازم تحط متغير بيئة BOT_TOKEN بتوكن البوت.")

    start_health_server()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(CallbackQueryHandler(on_bg_choice, pattern=r"^bg:"))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
