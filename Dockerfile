FROM python:3.12-slim
WORKDIR /service
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /service/data
ENV DB_PATH=/service/data/jobs.db SEED=42
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
