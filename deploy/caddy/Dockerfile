# Caddy with the deSEC DNS plugin, so it can get Let's Encrypt certificates via
# the DNS-01 challenge without the box being reachable from the internet.
# Base images pinned by digest; plugin pinned by version (Go checks its module hashes).

FROM caddy:2.11.8-builder-alpine@sha256:e42a64b3bc70aa3a1387bde85b930e507a378fecae5852d5bfde9dcd37d0b999 AS build
# caddy-ratelimit has no recent release tag; pinned to a reviewed commit.
RUN xcaddy build \
    --with github.com/caddy-dns/desec@v1.1.0 \
    --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db

FROM caddy:2.11.8-alpine@sha256:f6228111e5fafa1a5238258d0858e103f10599b8d1659a146e7726681a002bb9
COPY --from=build /usr/bin/caddy /usr/bin/caddy
# xcaddy marks the binary cap_net_bind_service=ep. We listen on 8443 and run with
# cap_drop: [ALL] + no-new-privileges, which refuses to exec such a binary. Strip it.
RUN setcap -r /usr/bin/caddy
