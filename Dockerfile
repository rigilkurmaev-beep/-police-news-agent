FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .
COPY vk_monitor.py .
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
