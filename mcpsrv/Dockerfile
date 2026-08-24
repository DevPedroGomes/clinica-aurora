# Servidor MCP da Clinica Aurora — runtime non-root, healthcheck em 127.0.0.1.
#
# A pasta se chama `mcpsrv` e nao `mcp` por higiene, nao por bug: MEDIDO, uma
# pasta `mcp/` sem __init__.py NAO sombreia o SDK, porque o import do Python so
# usa namespace package quando nenhum pacote regular e encontrado no path. O
# risco e latente: basta alguem adicionar um `__init__.py` ali, e a partir dai
# `from mcp.server.fastmcp import ...` passa a resolver para a nossa pasta e o
# erro so aparece dentro do container. Nome diferente elimina a classe toda.
#
# O contexto de build e a RAIZ do repo, nao esta pasta: a imagem precisa do
# codigo de dominio que mora em `agent/src`, que e compartilhado com o agente e
# nunca copiado.
FROM python:3.12-slim AS builder

WORKDIR /build
COPY mcpsrv/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10003 appuser
WORKDIR /app

COPY --from=builder /install /usr/local
COPY mcpsrv/server.py mcpsrv/healthcheck.py ./mcpsrv/
# So o dominio. `src/agent.py` e `src/api.py` ficam de fora: dependem de
# anthropic e de fastapi, que nao estao neste conjunto e nao sao necessarios.
COPY agent/src/__init__.py ./agent/src/__init__.py
COPY agent/src/policy ./agent/src/policy
COPY agent/src/tools ./agent/src/tools
COPY agent/src/adapters ./agent/src/adapters

USER appuser
EXPOSE 8080

# Ver mcpsrv/healthcheck.py para o porque de 406 contar como vivo.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "/app/mcpsrv/healthcheck.py"]

CMD ["uvicorn", "mcpsrv.server:app", "--host", "0.0.0.0", "--port", "8080"]
