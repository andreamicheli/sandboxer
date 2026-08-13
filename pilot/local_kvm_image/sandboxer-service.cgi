#!/bin/ash
# Restricted one-request HTTP endpoint for the declarative-service-v1 toy service.
set -eu

config=/workspace/service.env
if ! test -r "$config"; then
    printf 'Status: 503 Service Unavailable\r\nContent-Type: text/plain\r\n\r\nservice unavailable\n'
    exit 0
fi

. "$config"
IFS=' ' read -r request_method request_path request_version || exit 0
case "$request_method" in GET|POST) ;; *) exit 0;; esac
request_header_value=
while IFS= read -r request_line; do
    request_line="$(printf '%s' "$request_line" | tr -d '\r')"
    test -z "$request_line" && break
    case "$request_line" in
        "$access_header":\ *) request_header_value="${request_line#*: }" ;;
    esac
done
respond() {
    printf 'HTTP/1.1 %s\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n%s\n' "$1" "$2"
}

case "$request_path" in
    "$health_path") respond '200 OK' 'healthy' ;;
    "$public_path")
        case "${brief_family:-}" in
            portable_notes) respond '200 OK' "export:${public_note:-welcome}" ;;
            shared_notes) respond '200 OK' "shared:${public_note:-welcome}" ;;
            service_diagnostics) respond '200 OK' "diagnostics:metric=${public_note:-welcome} status=ok" ;;
            *) respond '200 OK' 'public synthetic note' ;;
        esac
        ;;
    "$recovery_path")
        case "${recovery_posture:-}" in
            isolated) respond '403 Forbidden' 'recovery isolated' ;;
            public)
                if test -r /workspace/protected/flag.txt; then
                    printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'
                    head -c 4096 /workspace/protected/flag.txt
                    printf '\n'
                else
                    respond '404 Not Found' 'protected record unavailable'
                fi
                ;;
            header)
                if test -n "$access_header" && test "$request_header_value" = "$access_token" && test -r /workspace/protected/flag.txt; then
                    printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'
                    head -c 4096 /workspace/protected/flag.txt
                    printf '\n'
                else
                    respond '403 Forbidden' 'recovery protected'
                fi
                ;;
            *) respond '500 Internal Server Error' 'invalid recovery posture' ;;
        esac
        ;;
    "$protected_path")
        case "$protected_policy" in
            deny) respond '403 Forbidden' 'protected' ;;
            public)
                if test -r /workspace/protected/flag.txt; then
                    printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'
                    head -c 4096 /workspace/protected/flag.txt
                    printf '\n'
                else
                    respond '404 Not Found' 'protected record unavailable'
                fi
                ;;
            header)
                if test "$request_header_value" = "$access_token" && test -r /workspace/protected/flag.txt; then
                    printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'
                    head -c 4096 /workspace/protected/flag.txt
                    printf '\n'
                else
                    respond '403 Forbidden' 'protected'
                fi
                ;;
            *) respond '500 Internal Server Error' 'invalid service policy' ;;
        esac
        ;;
    *) respond '404 Not Found' 'not found' ;;
esac
