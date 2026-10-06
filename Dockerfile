FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

ENV KILN_DB=/app/data/kiln.db \
    PORT=8000

EXPOSE 8000

CMD ["python", "app.py"]
