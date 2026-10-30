FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
ARG INSTALL_EXTRAS=observability
RUN pip install --no-cache-dir ".[${INSTALL_EXTRAS}]" && useradd --uid 10001 --create-home auditor
COPY controls ./controls
COPY config ./config
COPY .streamlit ./.streamlit
COPY data ./data
RUN mkdir /app/runtime && chown auditor:auditor /app/runtime
USER auditor
ENV AUDIT_DATABASE_URL=sqlite:////app/runtime/audit.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["audit-harness", "serve"]
