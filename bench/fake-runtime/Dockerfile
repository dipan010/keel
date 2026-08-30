FROM python:3.13-slim
RUN pip install --no-cache-dir fastapi uvicorn
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
