FROM nginx:1.27-alpine

# Runs as the image's own `nginx` user (uid 101) rather than root. nginx
# normally starts as root purely to bind port 80 and to write its pid and
# caches under /var; the container listens on 8080 instead and those paths are
# handed to uid 101, so no step needs privilege. The published port is
# unchanged - the host mapping in docker-compose.dev.yml does the translation.
RUN rm -f /etc/nginx/conf.d/default.conf \
 && sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf \
 && chown -R 101:0 /var/cache/nginx /etc/nginx/conf.d \
 && chmod -R g+rwX /var/cache/nginx /etc/nginx/conf.d

COPY web/nginx.conf /etc/nginx/conf.d/site.conf
COPY web/public/ /usr/share/nginx/html/

USER 101:0
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz > /dev/null || exit 1
