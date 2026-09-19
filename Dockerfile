FROM python:3.11-slim

# ffmpeg + مكتبات OpenCV الأساسية
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Render يمرر متغير PORT تلقائياً؛ القيمة هنا افتراضية للتشغيل المحلي فقط
ENV PORT=10000
EXPOSE 10000

CMD ["python", "bot.py"]
