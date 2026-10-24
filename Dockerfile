FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Конвертация идёт целиком в памяти, ffmpeg в образе не нужен.
CMD ["python", "bot.py"]
