#!/bin/sh

read -p "Enter database filename: " DB_FILENAME

if [ -z "$DB_FILENAME" ]; then
    echo "Database filename is required"
    exit 1
fi

docker exec -it saia-hub-db-1 bash -c "pg_restore -U kong -d kong -c -W -v /backup/$DB_FILENAME"

