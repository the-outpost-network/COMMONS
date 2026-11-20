# Egress allowlist proxy: the only way out for backend services.
# Matrix sends push notifications through it; everything except the hosts in
# allowlist.txt is refused. Base image pinned by digest, package by version.
FROM alpine:3.22@sha256:bfe7ceb9f09ec0566c48fb93356e45a47a00be5a1dd14fa5b909e7709c66b034
RUN apk add --no-cache tinyproxy=1.11.2-r0
COPY tinyproxy.conf allowlist.txt /etc/tinyproxy/
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
