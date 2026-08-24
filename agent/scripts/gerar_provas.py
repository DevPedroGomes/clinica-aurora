"""Gera `static/proof.json` com os números que a demo afirma na tela.

Por que isto existe: o case study do portfólio dizia "124 tests" enquanto a
suíte rodava 134. Ninguém mentiu — o número foi digitado uma vez e a suíte
continuou crescendo. Número de prova digitado à mão envelhece em silêncio, e uma
página que vende rigor não pode ter um número velho nela.

Roda no CI, depois da suíte e antes do build. O arquivo é baked na imagem e não
vai para o git: gerado é gerado. Se ele não existir, `/api/meta` responde só com
o que dá para saber em runtime e a barra esconde os campos que faltam — melhor
não mostrar do que mostrar errado.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "static" / "proof.json"


def _rodar(*args: str) -> str:
    r = subprocess.run(
        [sys.executable, "-m", *args],
        cwd=RAIZ,
        capture_output=True,
        text=True,
    )
    # pytest devolve != 0 quando a suíte falha, e aí não há prova nenhuma para
    # publicar. Falhar aqui é o certo: seria a página anunciando verde sobre
    # vermelho.
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"suíte falhou, nada a publicar: {' '.join(args)}")
    return r.stdout


def contar_testes() -> int:
    saida = _rodar("pytest", "tests/", "-q", "--collect-only")
    m = re.search(r"(\d+) tests? collected", saida)
    if not m:
        raise SystemExit(f"não achei a contagem de testes em:\n{saida[-2000:]}")
    return int(m.group(1))


def cobertura(alvo: str) -> float:
    saida = _rodar("pytest", "tests/", "-q", f"--cov={alvo}", "--cov-report=term")
    # A linha TOTAL é a última do relatório; o percentual é o último campo.
    for linha in reversed(saida.splitlines()):
        if linha.startswith("TOTAL"):
            return float(linha.split()[-1].rstrip("%"))
    raise SystemExit(f"não achei a linha TOTAL da cobertura de {alvo}")


def main() -> None:
    sys.path.insert(0, str(RAIZ))
    from src.agent import MODEL, PROMPT_ID
    from src.policy.engine import PolicyEngine
    from src.pricing import TABELA

    provas = {
        "testes": contar_testes(),
        "cobertura_total": cobertura("src"),
        "cobertura_policy": cobertura("src/policy"),
        "modelo": MODEL,
        "prompt_id": PROMPT_ID,
        "rules_version": PolicyEngine().version,
        "rates_version": TABELA.version,
    }
    DESTINO.write_text(json.dumps(provas, indent=2) + "\n", "utf-8")
    print(json.dumps(provas, indent=2))


if __name__ == "__main__":
    main()
