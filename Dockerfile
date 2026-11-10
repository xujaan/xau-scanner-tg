FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py chart.py reconcile.py ./

RUN useradd --create-home --uid 10001 bot && chown -R bot:bot /app
USER bot

CMD ["python", "-u", "bot.py"]
