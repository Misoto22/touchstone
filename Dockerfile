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
#
# The runtime directory is a private manifest with no `bin` of its own, so the
# executable lands in its `node_modules/.bin` and that is what goes on PATH.
# `npm link` published the empty wrapper instead and left the image without a
# working CLI - an image that builds, starts, and cannot audit anything.
#
# `--ignore-scripts` is deliberate and has a consequence: Claude Code fetches
# its platform binary from a postinstall script, so skipping every script means
# running that one by hand afterwards. This mirrors what the hosted install
# stage already does.
COPY agent-runtime /opt/touchstone/agent-runtime
ARG TOUCHSTONE_ENGINE=claude
ENV TOUCHSTONE_ENGINE=${TOUCHSTONE_ENGINE}
RUN cd "/opt/touchstone/agent-runtime/${TOUCHSTONE_ENGINE}" \
 && npm ci --ignore-scripts --include=optional --no-audit --no-fund \
 && if [ "${TOUCHSTONE_ENGINE}" = "claude" ]; then \
      node node_modules/@anthropic-ai/claude-code/install.cjs; \
    fi \
 && test -x "node_modules/.bin/${TOUCHSTONE_ENGINE}"
ENV PATH="/opt/touchstone/agent-runtime/claude/node_modules/.bin:/opt/touchstone/agent-runtime/codex/node_modules/.bin:${PATH}"

COPY . /opt/touchstone/src
RUN pip install --no-cache-dir /opt/touchstone/src

# The repository is mounted, not baked in, so the image says nothing about
# which repository it audits.
WORKDIR /repository
# Nothing in a container's log is worth having late. The entrypoint sets line
# buffering on its own streams; this covers every subprocess it starts.
ENV PYTHONUNBUFFERED=1
ENV TOUCHSTONE_CONTAINER_INTERVAL_SECONDS=900

# `run-due` evaluates the durable schedules itself, so this is a wake signal
# and not a second clock.
ENTRYPOINT ["python", "-m", "touchstone.container"]
