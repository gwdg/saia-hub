#!/bin/sh

DB_FILENAME="db_$(date +'%Y%m%d').tar"

read -p "Enter database filename (default: $DB_FILENAME): " USER_INPUT

DB_FILENAME=${USER_INPUT:-$DB_FILENAME}

docker exec -it saia-hub-db-1 bash -c "pg_dump -U kong -d kong -F t > /backup/$DB_FILENAME"

