#!/bin/bash
# Runs in the guest as `agent` on every provision; must stay idempotent.
set -euo pipefail

# SynapseWebClient's docker-compose.yml bind-mounts ${HOME}/.m2/settings.xml.
# If the file is missing, Docker creates a root-owned *directory* there and
# Maven then fails with "Non-readable settings ... (Is a directory)".
# Seed the shared dev-environment settings (no credentials). Host credentials are never copied in.
M2="$HOME/.m2"
SETTINGS="$M2/settings.xml"
mkdir -p "$M2"

# Repair the Docker-created directory. rmdir refuses non-empty directories,
# so nothing of value can be removed here.
if [ -d "$SETTINGS" ] && [ ! -L "$SETTINGS" ]; then
  sudo rmdir "$SETTINGS"
fi

# Never overwrite an existing file.
if [ ! -e "$SETTINGS" ]; then
  umask 077
  cat > "$SETTINGS" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0
                      http://maven.apache.org/xsd/settings-1.0.0.xsd">
  <localRepository/>
  <interactiveMode/>
  <usePluginRegistry/>
  <offline/>
  <pluginGroups/>
  <servers/>
  <mirrors/>
  <proxies/>
  <profiles>
    <profile>
      <id>dev-environment</id>
      <activation>
        <activeByDefault>true</activeByDefault>
      </activation>
      <properties>
      <org.sagebionetworks.portal.endpoint>http://127.0.0.1:8888/Portal.html</org.sagebionetworks.portal.endpoint>
        <org.sagebionetworks.repositoryservice.endpoint>https://repo-dev.dev.sagebase.org/repo/v1</org.sagebionetworks.repositoryservice.endpoint>
        <!-- insert properties described below here -->
        <!-- If you want to point to a different stack (production, staging, local), comment out the above and uncomment one of the below -->
        <!-- <org.sagebionetworks.repositoryservice.endpoint>https://repo-staging.prod.sagebase.org/repo/v1</org.sagebionetworks.repositoryservice.endpoint> -->
        <!-- <org.sagebionetworks.repositoryservice.endpoint>https://repo-prod.prod.sagebase.org/repo/v1</org.sagebionetworks.repositoryservice.endpoint> -->
        <!-- <org.sagebionetworks.repositoryservice.endpoint>http://localhost:8080/services-repository-develop-SNAPSHOT/repo/v1</org.sagebionetworks.repositoryservice.endpoint> -->
        <!-- Uncomment the property below to limit GWT permutations and speed up build time -->
        <!-- <gwt.module.suffix>Debug</gwt.module.suffix> -->
      </properties>
    </profile>
  </profiles>
  <activeProfiles/>
</settings>
XML
fi

# The same failed mount also leaves an empty settings.xml directory inside the
# `maven-repo` named volume (mounted at /root/.m2), which then blocks the file
# mount ("not a directory"). rmdir only removes an empty directory.
if docker volume inspect maven-repo >/dev/null 2>&1; then
  docker run --rm -v maven-repo:/m alpine sh -c '[ ! -d /m/settings.xml ] || rmdir /m/settings.xml'
fi

# Vite/chokidar in the dev stack watches the whole project including
# .pnpm-store, which exceeds the default inotify watch limit. This is the
# guest VM's own kernel, so raising it does not affect the host.
sudo tee /etc/sysctl.d/99-asbx-inotify.conf >/dev/null <<'CONF'
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512
CONF
sudo sysctl --system >/dev/null

# The statechart skill (used by the formal verification skills) installs TLC and
# Java through mise on first run. Install them now, from the pins in the mounted
# skill, so the first model check needs no network and the pins never drift.
STATECHART_RUN="$HOME/.agents/skills/statechart/scripts/run.ts"
if [ -f "$STATECHART_RUN" ]; then
  TLC_TOOL=$(grep -oP '^\s*"\Khttp:tla2tools\[[^"]+' "$STATECHART_RUN")
  JAVA_TOOL=$(grep -oP '^const JAVA_TOOL = "\K[^"]+' "$STATECHART_RUN")
  (cd /tmp && mise install --yes "$JAVA_TOOL" "$TLC_TOOL")
fi
