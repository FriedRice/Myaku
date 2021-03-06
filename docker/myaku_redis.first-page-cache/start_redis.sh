#!/bin/bash

set -e

exec redis-server $REDIS_CONF_FILE \
    --requirepass "$(cat $MYAKU_FIRST_PAGE_CACHE_PASSWORD_FILE)" \
    --dir "$REDIS_DATA_DIR"
