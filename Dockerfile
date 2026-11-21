# Blast Radius selector. Same base image the commits were labeled in: its
# Python decides which sqlglot tests run and how.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.lock /app/requirements.lock
RUN pip install -q -r /app/requirements.lock

# sqlglot at the pinned commit, without its git history or docs
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py ${SQLGLOT_SHA}

# Bytecode, so nothing is compiled at run time:
#  - in place, for the copy the parent process imports
#  - under /opt/pyc for the two patchable work trees the run creates in /tmp
#    (same paths and mtimes, so their .pyc files validate)
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests >/dev/null \
 && for i in 0 1; do \
      mkdir -p /tmp/w$i && cp -a /opt/sqlglot/. /tmp/w$i/ && find /tmp/w$i -name __pycache__ -prune -exec rm -rf {} + ; \
      python -c "import sys, compileall; sys.pycache_prefix='/opt/pyc'; [compileall.compile_dir(f'/tmp/w$i/{d}', quiet=2) for d in ('sqlglot', 'tests')]"; \
      rm -rf /tmp/w$i; \
    done

COPY src /app/src
COPY data /app/data
RUN python -m compileall -q /app/src >/dev/null \
 && cd /app && PYTHONPATH=/app/src python -m blast.prebuild /opt/sqlglot /app/prebuilt.pkl /app/data/map.json.gz \
 && chmod -R a+rX /app /opt

WORKDIR /app
ENV PYTHONPATH=/app/src
CMD ["python", "-m", "blast.main"]
