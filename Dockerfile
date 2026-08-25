# Touchstone's own runtime and nothing a project owns.
#
# A repository's Validation Gates run its own toolchain, and no single image
# can carry every version of every one of them - one of the stacks Touchstone
# detects cannot run on Linux at all. So this image stays thin, and a
# repository that needs a toolchain derives from it:
#
#     FROM ghcr.io/misoto22/touchstone:0.1.2
#     RUN apt-get update && apt-get install -y --no-install-recommends <toolchain>
#
# One repository per container. The container sees one checkout, one state
# volume, and one credential set; that boundary is what keeps a fleet's
# repositories from sharing a blast radius.
FROM python:3.12-slim-bookworm

# git is Touchstone's own dependency: it reads history, builds worktrees, and
# stages the diff a session produced. gh is how publication reaches the forge.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh nodejs \
 && apt-get clean

# The Agent CLI comes from the committed lockfile, never from a floating tag:
# the version this image runs is the version the repository recorded.
COPY agent-runtime /opt/touchstone/agent-runtime
ARG TOUCHSTONE_ENGINE=claude
RUN cd "/opt/touchstone/agent-runtime/${TOUCHSTONE_ENGINE}" \
 && npm ci --ignore-scripts --omit=dev \
 && npm link

COPY . /opt/touchstone/src
RUN pip install --no-cache-dir /opt/touchstone/src

# The repository is mounted, not baked in, so the image says nothing about
# which repository it audits.
WORKDIR /repository
ENV TOUCHSTONE_CONTAINER_INTERVAL_SECONDS=900

# `run-due` evaluates the durable schedules itself, so this is a wake signal
# and not a second clock.
ENTRYPOINT ["python", "-m", "touchstone.container"]
