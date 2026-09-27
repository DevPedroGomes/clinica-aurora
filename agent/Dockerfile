# Agente da Clínica Aurora — runtime non-root, healthcheck em 127.0.0.1.
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10002 appuser
WORKDIR /app

COPY --from=builder /install /usr/local
COPY src ./src
COPY static ./static

# /data nasce na imagem com o dono certo, ANTES do USER: o Docker semeia o
# volume vazio a partir do path da imagem, permissão inclusa. Sem isto o volume
# monta como root e o teto diario — que recusa em vez de falhar aberto — mataria
# a demo em silencio no primeiro acesso.
RUN mkdir -p /data && chown appuser:appuser /data
VOLUME /data

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

# Sem `server: uvicorn` na resposta: o cabecalho so informa a pilha a quem procura
# alvo, e nenhum cliente depende dele.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
