FROM debian:12-slim

ARG REALM_VERSION=v2.9.6
ARG TARGETARCH

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download realm binary from GitHub Releases (no auth needed)
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
      x86_64)  TRIPLE="x86_64-unknown-linux-gnu" ;; \
      aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;; \
      armv7l)  TRIPLE="armv7-unknown-linux-gnueabihf" ;; \
      *)        echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL \
      "https://github.com/zhboner/realm/releases/download/${REALM_VERSION}/realm-${TRIPLE}.tar.gz" \
      | tar -xz -C /usr/local/bin realm && \
    chmod +x /usr/local/bin/realm

RUN mkdir -p /etc/realm/rules /etc/realm/health /etc/realm/panel && \
    echo '{"network":{},"endpoints":[]}' > /etc/realm/config.json

ENTRYPOINT ["/usr/local/bin/realm"]
CMD ["-c", "/etc/realm/config.json"]
