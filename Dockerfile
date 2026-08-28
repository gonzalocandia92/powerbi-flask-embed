FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY requirements/ /app/requirements/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 2052

CMD ["python", "run.py"]
