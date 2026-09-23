FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY static static
COPY templates templates

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
