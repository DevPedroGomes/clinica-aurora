"""Conversas de verdade com o modelo, para medir em que idioma o agente responde.

Roda o agente local contra o ERP local (em processo, sem rede além da chamada ao
provedor) e conta quantas respostas saíram em português. Gasta dinheiro: cada
conversa faz de 2 a 8 chamadas pagas. Por isso não roda na suíte padrão.

    cd agent
    ANTHROPIC_API_KEY=... ../.venv/bin/python scripts/avaliar_idioma.py --idioma en --conversas 10
    ANTHROPIC_API_KEY=... ../.venv/bin/python scripts/avaliar_idioma.py --idioma pt --conversas 3

Sem a variável no ambiente, lê `ANTHROPIC_API_KEY` do `.env` na raiz do repo.

A classificação das respostas é INDEPENDENTE do detector que o agente usa para
conferir o idioma (`src/detector_idioma.py`): outra lista de palavras e o sinal
de acento, com os nomes do cadastro tirados antes. Medir com a mesma régua que a
correção usa provaria só que a régua concorda consigo mesma.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ.parent))

CONVERSAS: dict[str, list[list[str]]] = {
    "en": [
        ["Move Ana's appointment to 4pm", "yes"],
        ["Cancel João's appointment", "the patient is sick", "yes"],
        ["Move Ana's appointment to Sunday at 10am"],
        ["Book Marta with Dr. Helena the day after tomorrow at 10am", "ok"],
        ["Move João's appointment to 2pm the same day", "sure"],
        ["What's on Dr. Bruno's schedule?"],
        ["Open a 30 minute slot for Dr. Helena at 3pm, same day as Ana's", "go ahead"],
        ["Book Maria Teste tomorrow at 10am"],
        ["Write me a poem about the sea"],
        ["!caos", "Move Ana's appointment to 5pm", "yes"],
    ],
    "pt": [
        ["Remarque a consulta da Ana para as 16h", "sim"],
        ["Cancele a consulta do João", "paciente doente", "sim"],
        ["O que tem na agenda do Dr. Bruno?"],
    ],
}

# Nomes do cadastro saem antes de classificar: "João" numa resposta em inglês
# não é português.
NOMES = re.compile(
    r"Ana Ribeiro|João Meireles|Joao Meireles|Marta Lins|Bruno Salles|"
    r"Helena Costa|Dra\.|Dr\.|Clínica Aurora|João|Marta|Ana|Helena|Bruno",
)
MARCAS_PT = {
    "não",
    "você",
    "está",
    "estão",
    "consulta",
    "consultas",
    "horário",
    "horários",
    "remarquei",
    "cancelei",
    "marquei",
    "amanhã",
    "hoje",
    "posso",
    "pode",
    "seguir",
    "quer",
    "às",
    "obrigado",
    "pronto",
    "já",
    "também",
    "motivo",
    "expediente",
    "paciente",
    "agendada",
    "agendado",
    "gravei",
    "alteração",
    "nenhuma",
    "qual",
    "isso",
    "uma",
    "com",
    "para",
}
ACENTO_PT = re.compile(r"[ãõç]|ção|ções")


def parece_portugues(texto: str) -> bool:
    limpo = NOMES.sub(" ", texto).lower()
    palavras = re.findall(r"[a-záàâãéêíóôõúüçñ]+", limpo)
    marcas = sum(p in MARCAS_PT for p in palavras)
    return marcas >= 2 or (marcas >= 1 and bool(ACENTO_PT.search(limpo)))


@dataclass
class Resultado:
    idioma: str
    conversas: int = 0
    respostas: int = 0
    em_portugues: int = 0
    reescritas: int = 0
    reescritas_sem_sucesso: int = 0
    usd: float = 0.0
    transcricoes: list[dict[str, Any]] = field(default_factory=list)

    def resumo(self) -> dict[str, Any]:
        return {
            "idioma_da_sessao": self.idioma,
            "conversas": self.conversas,
            "respostas": self.respostas,
            "respostas_em_portugues": self.em_portugues,
            "reescritas_de_idioma": self.reescritas,
            "reescritas_sem_sucesso": self.reescritas_sem_sucesso,
            "usd": round(self.usd, 4),
        }


def _carregar_chave() -> None:
    if os.getenv("ANTHROPIC_API_KEY"):
        return
    env = RAIZ.parent / ".env"
    if env.exists():
        for linha in env.read_text("utf-8").splitlines():
            if linha.startswith("ANTHROPIC_API_KEY=") and linha.split("=", 1)[1]:
                os.environ["ANTHROPIC_API_KEY"] = linha.split("=", 1)[1].strip()
                return
    raise SystemExit("ANTHROPIC_API_KEY ausente (ambiente ou .env na raiz).")


async def avaliar(idioma: str, conversas: int, cliente: Any = None) -> Resultado:
    """Roda `conversas` roteiros na sessão `idioma`. `cliente` troca o SDK (teste)."""
    import httpx
    from anthropic import AsyncAnthropic

    from erp.app import main as erp_main
    from src.adapters.erp import ErpAdapter
    from src.agent import Agente
    from src.flows.state_machine import Conversation
    from src.policy.engine import PolicyEngine
    from src.tools.registry import AgentContext

    if cliente is None:
        _carregar_chave()
        cliente = AsyncAnthropic()
    resultado = Resultado(idioma=idioma)
    agente = Agente(cliente)
    transporte = httpx.ASGITransport(app=erp_main.app)

    async with httpx.AsyncClient(transport=transporte, base_url="http://erp") as erp:
        for roteiro in CONVERSAS[idioma][:conversas]:
            tenant = f"demo-eval{uuid.uuid4().hex[:8]}"
            ctx = AgentContext(
                conversation_id=tenant,
                tenant_id=tenant,
                policy=PolicyEngine(),
                erp=ErpAdapter("http://erp", tenant_id=tenant, client=erp),
                at=datetime.now(timezone.utc),
            )
            ctx.definir_idioma(idioma)
            conv = Conversation()
            transcricao: dict[str, Any] = {"turnos": []}
            for fala in roteiro:
                if fala == "!caos":
                    # A sessão é semeada na primeira leitura; o caos vem depois.
                    await erp.get("/consultas", headers={"X-Tenant-Id": tenant})
                    await erp.post(
                        "/admin/chaos",
                        params={"ligado": "true"},
                        headers={"X-Tenant-Id": tenant},
                    )
                    continue
                ctx.at = datetime.now(timezone.utc)
                turno: dict[str, Any] = {"pessoa": fala, "agente": [], "eventos": []}
                async for evento in agente.turno(conv, fala, ctx):
                    if evento.tipo == "message":
                        texto = evento.dado["texto"]
                        pt = parece_portugues(texto)
                        turno["agente"].append({"texto": texto, "portugues": pt})
                        resultado.respostas += 1
                        resultado.em_portugues += int(pt)
                    elif evento.tipo == "language_check":
                        resultado.reescritas += 1
                        resultado.reescritas_sem_sucesso += int(
                            not evento.dado["corrigido"]
                        )
                    if evento.tipo in {
                        "policy",
                        "tool_error",
                        "escalated",
                        "language_check",
                    }:
                        turno["eventos"].append({evento.tipo: evento.dado})
                transcricao["turnos"].append(turno)
            resultado.usd += ctx.custo_micros / 1_000_000
            resultado.conversas += 1
            resultado.transcricoes.append(transcricao)
    return resultado


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--idioma", choices=["en", "pt"], default="en")
    parser.add_argument("--conversas", type=int, default=10)
    parser.add_argument(
        "--saida", type=Path, help="grava resumo e transcrições em JSON"
    )
    parser.add_argument(
        "--exigir-zero",
        action="store_true",
        help="sai com erro se alguma resposta de sessão em inglês vier em português",
    )
    args = parser.parse_args()

    resultado = asyncio.run(avaliar(args.idioma, args.conversas))
    for i, t in enumerate(resultado.transcricoes, 1):
        print(f"\n--- conversa {i}")
        for turno in t["turnos"]:
            print(f"  pessoa: {turno['pessoa']}")
            for r in turno["agente"]:
                marca = "  [PT]" if r["portugues"] else ""
                print(f"  agente{marca}: {r['texto']}")
            for e in turno["eventos"]:
                print(f"    · {json.dumps(e, ensure_ascii=False)}")
    print("\n" + json.dumps(resultado.resumo(), ensure_ascii=False, indent=2))
    if args.saida:
        args.saida.write_text(
            json.dumps(
                {**resultado.resumo(), "transcricoes": resultado.transcricoes},
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
    if args.exigir_zero and args.idioma == "en" and resultado.em_portugues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
