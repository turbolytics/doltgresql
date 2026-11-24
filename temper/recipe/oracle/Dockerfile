# Python for the oracles, and Java 21 for Elle (ligurio/elle-cli 0.1.11). The
# JDK, not the JRE: Jepsen needs the jdk.random module.
FROM eclipse-temurin:21-jdk-jammy
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip unzip curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --no-cache-dir "psycopg[binary]==3.2.9" "pyyaml==6.0.2" \
 && curl -fsSL -o /tmp/elle.zip https://github.com/ligurio/elle-cli/releases/download/0.1.11/elle-cli-bin-0.1.11.zip \
 && mkdir /elle && unzip -q /tmp/elle.zip -d /elle && rm /tmp/elle.zip \
 && find /elle -name 'elle-cli*standalone*.jar' -exec cp {} /elle/elle-cli.jar \; \
 && test -f /elle/elle-cli.jar \
 && ln -sf /usr/bin/python3 /usr/local/bin/python
COPY . /oracle
WORKDIR /oracle
