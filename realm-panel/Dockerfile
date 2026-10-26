# Build context is the repo root (see docker-compose.yml) so the agent
# files can be bundled and served to remote nodes at /agent/*.
FROM python:3.12-slim

ARG REALM_VERSION=v2.9.6

WORKDIR /panel

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    curl wget tar jq bc netcat-openbsd ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY realm-panel/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# realm binaries for remote nodes, so nodes that can't reach GitHub can
# still install from the panel. Failures are tolerated: install.sh falls
# back to GitHub.
RUN mkdir -p /panel/agent_dist/realm && cd /panel/agent_dist/realm && \
    for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl armv7-unknown-linux-musleabihf; do \
      curl -fsSL --retry 3 -o "realm-$t.tar.gz" \
        "https://github.com/zhboner/realm/releases/download/${REALM_VERSION}/realm-$t.tar.gz" \
        || { echo "WARN: could not prefetch realm-$t"; rm -f "realm-$t.tar.gz"; }; \
    done

COPY realm-agent/agent.py realm-agent/install.sh /panel/agent_dist/
COPY realm-panel/ .

RUN mkdir -p /etc/realm/rules /etc/realm/health /etc/realm/panel

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "60", "wsgi:application"]
