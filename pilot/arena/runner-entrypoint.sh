#!/bin/sh
set -eu

mkdir -p /arena/service /arena/runtime /arena/logs /arena/submissions
if [ ! -f /arena/service/service.py ]; then
  cp -R /opt/pristine/. /arena/service/
fi

arena-restart
exec tail -f /dev/null
