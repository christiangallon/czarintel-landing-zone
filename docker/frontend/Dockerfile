# syntax=docker/dockerfile:1.7
###############################################################################
# CzarIntel Frontend — health dashboard served by unprivileged Nginx
#
#   docker build -t czarintel/frontend:dev docker/frontend
#   docker run --rm -p 8080:8080 -e API_UPSTREAM=http://host.docker.internal:8000 czarintel/frontend:dev
###############################################################################

# ---------- Stage 1: validate & minify static assets -------------------------
FROM node:20-alpine AS build
WORKDIR /src
RUN npm install --global --no-audit --no-fund html-minifier-terser@7.2.0
COPY index.html .
RUN mkdir -p /dist \
 && html-minifier-terser \
      --collapse-whitespace --remove-comments --minify-css true --minify-js true \
      -o /dist/index.html index.html

# ---------- Stage 2: runtime (non-root nginx on :8080) ------------------------
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

LABEL org.opencontainers.image.title="czarintel-frontend" \
      org.opencontainers.image.vendor="CzarIntel" \
      org.opencontainers.image.licenses="MIT"

# Upstream api-service (Kubernetes Service DNS by default)
ENV API_UPSTREAM=http://api-service:80

# Nginx config template — envsubst is applied at start by the official entrypoint
COPY --chown=101:101 <<'EOF' /etc/nginx/templates/default.conf.template
server_tokens off;

server {
    listen       8080;
    server_name  _;
    root         /usr/share/nginx/html;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'" always;

    location = /healthz {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }

    location ~ ^/(api/|health$) {
        proxy_pass         ${API_UPSTREAM};
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Request-ID      $request_id;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_connect_timeout 3s;
        proxy_read_timeout    10s;
    }

    location / {
        try_files $uri /index.html;
    }
}
EOF

COPY --from=build --chown=101:101 /dist/index.html /usr/share/nginx/html/index.html

USER 101
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
