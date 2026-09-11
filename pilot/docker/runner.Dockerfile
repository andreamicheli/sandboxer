FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl dnsutils iproute2 jq netcat-openbsd nmap procps tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/bash arena \
    && mkdir -p /opt/pristine /arena \
    && chown -R arena:arena /opt/pristine /arena /home/arena

COPY --chown=arena:arena arena/pristine/ /opt/pristine/
COPY --chown=root:root arena/runner-entrypoint.sh /usr/local/bin/runner-entrypoint
COPY --chown=root:root arena/arena-restart /usr/local/bin/arena-restart
COPY --chown=root:root arena/arena-reset /usr/local/bin/arena-reset
COPY --chown=root:root arena/arena-submit /usr/local/bin/arena-submit
COPY --chown=root:root docker/runner-control /usr/local/bin/runner-control
COPY --chown=root:root docker/toy-service.py /usr/local/libexec/toy-service.py
RUN chmod 0755 /usr/local/bin/runner-entrypoint /usr/local/bin/arena-restart /usr/local/bin/arena-reset /usr/local/bin/arena-submit /usr/local/bin/runner-control /usr/local/libexec/toy-service.py

USER arena
WORKDIR /arena
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/runner-entrypoint"]
