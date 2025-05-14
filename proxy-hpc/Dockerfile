FROM python:3.12.8-slim-bookworm
WORKDIR /root
# Install OpenSSH client and create SSH config
RUN apt-get update && apt-get install -y openssh-client
RUN pip install -U pip
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt 
CMD cd /root && python proxy.py
