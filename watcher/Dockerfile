FROM python:3.12-alpine

WORKDIR /app
COPY watcher.py /app/watcher.py
COPY launcher.sh /app/launcher.sh

RUN chmod +x /app/launcher.sh

CMD ["sh", "/app/launcher.sh"]
