FROM nginx:1.27-alpine
RUN rm -f /etc/nginx/conf.d/default.conf
COPY web/nginx.conf /etc/nginx/conf.d/site.conf
COPY web/public/ /usr/share/nginx/html/
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/healthz > /dev/null || exit 1
