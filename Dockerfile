# Official DeerFlow backend image plus
#   - the optional OpenSandbox SDK (the published image only ships the postgres extra)
#   - deerflow_ext.opensandbox_skills, an OpenSandboxProvider that also uploads the
#     skills projection and restores thread files into OpenSandbox remotes.
#
# The same image doubles as the OpenSandbox sandbox image: its venv already holds
# the packages bundled skills import (LocalSandbox runs skills in this environment).
#
# config.yaml:
#   sandbox:
#     use: deerflow_ext.opensandbox_skills:SkillAwareOpenSandboxProvider
#     image: <this image, same tag as the gateway>
ARG DEERFLOW_VERSION=2.2.0

FROM ghcr.io/bytedance/deer-flow-backend:v${DEERFLOW_VERSION} AS image
# Sandbox shell tools: curl/git for bundled skill scripts; ps, zip/unzip and jq
# for everyday agent shell work.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq procps unzip zip \
 && rm -rf /var/lib/apt/lists/*
# Harness `opensandbox` extra at the version locked by this release; --inexact
# keeps every package the official image already has. uv venvs ship without pip,
# so add it: an agent's `pip install` must land where `python` resolves.
RUN cd /app/backend \
 && uv sync --locked --inexact --all-packages --extra opensandbox \
 && uv pip install --python .venv/bin/python pip
# Sandbox commands get `python`/`pip` from the venv that holds the skill packages.
ENV PATH="/app/backend/.venv/bin:${PATH}"
# The gateway runs `cd backend && PYTHONPATH=.`, which makes this importable.
COPY opensandbox_skills.py /app/backend/deerflow_ext/opensandbox_skills.py

# The provider overrides private upstream hooks: test it against this exact release.
FROM image AS test
COPY test_opensandbox_skills.py /tmp/test_opensandbox_skills.py
RUN cd /app/backend \
 && uv pip install --python .venv/bin/python pytest \
 && .venv/bin/python -m pytest -q -p no:cacheprovider /tmp/test_opensandbox_skills.py \
 && touch /tmp/tests-passed

FROM image
# Depending on the test stage makes BuildKit run it; a failing test fails the build.
COPY --from=test /tmp/tests-passed /tmp/.opensandbox-skills-tests-passed
