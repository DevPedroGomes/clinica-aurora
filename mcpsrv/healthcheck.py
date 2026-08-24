"""Sinal de vida do servidor MCP.

Um GET simples em /mcp responde 406: o protocolo exige cabecalhos de Accept que
este check nao manda. Isso e proposital e util — 406 e resposta DO APP, entao
distingue "processo de pe" de "processo morto", que e tudo o que um healthcheck
precisa saber. Qualquer status HTTP conta como vivo; so erro de conexao mata.
"""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8080/mcp", timeout=4)
except urllib.error.HTTPError:
    pass  # respondeu, logo esta vivo
except Exception:
    sys.exit(1)
sys.exit(0)
