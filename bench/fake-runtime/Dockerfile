FROM python:3.13-slim
RUN pip install --no-cache-dir fastapi uvicorn
COPY app.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
