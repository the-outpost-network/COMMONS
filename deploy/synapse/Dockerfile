# Synapse: official image, plus a /data directory owned by the non-root
# user so the named volume inherits that ownership.
FROM ghcr.io/element-hq/synapse:v1.164.2@sha256:f0df87cff259872637b4b5e5174b9977e5e3046dd421505407803b5e45c5f842
RUN mkdir -p /data && chown 10001:10001 /data
