#!/bin/sh
set -eu

mkdir -p /arena/service /arena/runtime /arena/logs /arena/submissions /arena/protected
if [ ! -f /arena/service/service.py ]; then
  cp -R /opt/pristine/. /arena/service/
fi
if [ ! -f /arena/service.env ]; then
  cat >/arena/service.env <<'EOF'
schema_version=sandboxer.service-spec.v1
health_path=/cgi-bin/service.cgi?route=health
public_path=/cgi-bin/service.cgi?route=public
protected_path=/cgi-bin/service.cgi?route=protected
protected_policy=deny
access_header=
access_token=
recovery_path=/cgi-bin/service.cgi?route=recovery
recovery_posture=isolated
EOF
  chmod 0600 /arena/service.env
fi
chmod 0700 /arena/protected

arena-restart
exec tail -f /dev/null
