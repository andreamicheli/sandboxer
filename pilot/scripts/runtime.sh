#!/bin/sh
set -eu

profile=sandboxer
export PATH="$HOME/.local/bin:$PATH"

case "${1:-}" in
  start)
    colima start "$profile" \
      --cpu 6 \
      --memory 10 \
      --disk 24 \
      --runtime docker \
      --vm-type vz \
      --mount-type virtiofs \
      --mount none \
      --port-forwarder none
    ;;
  stop)
    colima stop "$profile"
    ;;
  status)
    colima status "$profile"
    ;;
  *)
    echo "usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac
