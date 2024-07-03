FROM python:3.11-slim-buster
ENV PORT=8000
WORKDIR /app
RUN pip install -U pip
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt 
COPY . .
CMD python -m uvicorn proxy:app --workers 1 --host 0.0.0.0 --port "$PORT" --log-config ./log_conf.yaml