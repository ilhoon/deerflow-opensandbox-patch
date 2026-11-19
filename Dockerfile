# Official DeerFlow backend image plus
#   - the optional OpenSandbox SDK (the published image only ships the postgres extra)
#   - deerflow_ext.opensandbox_skills, an OpenSandboxProvider that also uploads the
#     skills projection and restores thread files into OpenSandbox remotes.
#
# config.yaml:
#   sandbox:
#     use: deerflow_ext.opensandbox_skills:SkillAwareOpenSandboxProvider
ARG DEERFLOW_VERSION=2.2.0

FROM ghcr.io/bytedance/deer-flow-backend:v${DEERFLOW_VERSION} AS image
# Harness `opensandbox` extra at the version locked by this release; --inexact
# keeps every package the official image already has.
RUN cd /app/backend && uv sync --locked --inexact --all-packages --extra opensandbox
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
