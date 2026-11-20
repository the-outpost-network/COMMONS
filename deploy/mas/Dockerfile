# MAS with one template replaced: the registration-token step of sign-up fills in
# the code from a COMMONS invite link by itself (see registration_token.html).
#
# The build stops if MAS' own template is not the one ours was made from, so a
# MAS upgrade can't silently drop or break the change: compare the new upstream
# file with ours, update ours and the checksum below.
ARG MAS_IMAGE=ghcr.io/element-hq/matrix-authentication-service:1.28.0@sha256:494c67f08ee8344368f4ed336611dea02b26c4e058bf37a212d090d50b612659
ARG TEMPLATE=/usr/local/share/mas-cli/templates/pages/register/steps/registration_token.html

FROM ${MAS_IMAGE} AS mas

FROM alpine:3.22@sha256:bfe7ceb9f09ec0566c48fb93356e45a47a00be5a1dd14fa5b909e7709c66b034 AS check
ARG TEMPLATE
COPY --from=mas ${TEMPLATE} /upstream.html
COPY registration_token.html /registration_token.html
RUN echo "5780eaa38075f6d9f79b9e327661224518e13b3b28fae922d1300d70551ee10e  /upstream.html" | sha256sum -c -

FROM mas
ARG TEMPLATE
COPY --from=check /registration_token.html ${TEMPLATE}
