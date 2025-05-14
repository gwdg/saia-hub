FROM python:3.11-slim-buster
WORKDIR /root
RUN pip install -U pip
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt 
CMD cd /root && python proxy.py