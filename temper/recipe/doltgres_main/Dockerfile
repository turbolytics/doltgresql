# Doltgres built from the source this map is on, for the doltgres.main
# subject. It follows the repository's Dockerfile (DOLTGRES_VERSION=source),
# but builds for the architecture Docker targets: the upstream stage builds
# linux-amd64 only.
FROM golang:1.27-trixie AS build
ARG TARGETARCH
WORKDIR /src
COPY . .
# The module proxy sometimes drops a download mid-stream, so retry it.
RUN for i in 1 2 3; do go mod download && break; sleep 5; done \
 && go mod download \
 && ./postgres/parser/build.sh \
 && ./scripts/build_binaries.sh "linux-${TARGETARCH}" \
 && mv out/doltgresql-*/bin/doltgres /usr/local/bin/doltgres

FROM debian:trixie-slim
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends curl tini ca-certificates postgresql-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/bin/doltgres /usr/local/bin/doltgres
RUN /usr/local/bin/doltgres --version \
 && mkdir /docker-entrypoint-initdb.d \
 && mkdir -p /var/lib/doltgres && chmod 755 /var/lib/doltgres
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
VOLUME /var/lib/doltgres
EXPOSE 5432
WORKDIR /var/lib/doltgres
ENTRYPOINT ["tini", "-v", "--", "docker-entrypoint.sh"]
