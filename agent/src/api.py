"""A superfície pública da demo.

Sessão anônima com tenant isolado (padrão do voice_rag): cada visitante recebe
a própria agenda semeada e nunca vê a de outro. Sem login — cadastro afasta
visitante e não prova nada.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import budget
from .adapters.erp import ErpAdapter
from .agent import MODEL, Agente
from .flows.state_machine import Conversation
from .policy.engine import PolicyEngine
from .tools.registry import AgentContext

ERP_URL = os.getenv("ERP_URL", "http://erp:8080")
SESSAO_TTL = 2 * 60 * 60
MAX_SESSOES = 500
MENSAGEM_MAX = 500
RATE_POR_IP = 30  # mensagens por 10 minutos
# Sessoes NOVAS por IP na mesma janela. Sem isto, 500 GETs sem cookie em
# /api/estado enchiam `_sessoes` e o despejo do mais antigo apagava a conversa
# de todo visitante que estava no meio de uma. Um visitante de verdade abre uma.
NOVAS_SESSOES_POR_IP = 20
JANELA_RATE = 600

STATIC = Path(__file__).resolve().parent.parent / "static"

# O uvicorn configura os loggers dele e deixa o root intocado, cujo handler
# de último recurso corta tudo abaixo de WARNING. Sem esta linha o audit log
# de `registry.py` era emitido e descartado silenciosamente — pior que não ter,
# porque parecia existir.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
)

app = FastAPI(
    title="Clínica Aurora — demo", docs_url=None, redoc_url=None, openapi_url=None
)
agente = Agente()


@dataclass
class Sessao:
    tenant_id: str
    conversa: Conversation = field(default_factory=Conversation)
    ctx: AgentContext | None = None
    criada_em: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    vista: str | None = None
    """O carregamento de página que falou por último com esta sessão."""


_sessoes: dict[str, Sessao] = {}
_hits: dict[str, deque[float]] = defaultdict(deque)
_novas: dict[str, deque[float]] = defaultdict(deque)


def _limpar() -> None:
    agora = time.monotonic()
    for sid in [s for s, v in _sessoes.items() if agora - v.criada_em > SESSAO_TTL]:
        del _sessoes[sid]
    while len(_sessoes) > MAX_SESSOES:
        mais_antiga = min(_sessoes, key=lambda s: _sessoes[s].criada_em)
        del _sessoes[mais_antiga]


def _ip(x_real_ip: str | None) -> str:
    # x-real-ip é o que o Traefik seta a partir do peer TCP real. x-forwarded-for
    # é controlável pelo cliente — usar aquele daria a cada requisição uma cota nova.
    return x_real_ip or "sem-ip"


def _janela(hits: dict[str, deque[float]], ip: str, limite: int, msg: str) -> None:
    agora = time.monotonic()
    janela = hits[ip]
    while janela and agora - janela[0] > JANELA_RATE:
        janela.popleft()
    if not janela:
        # Sem isto o dicionário nunca encolhe: cada IP que passou por aqui uma
        # vez deixa uma chave viva para sempre. É vazamento lento, mas é o tipo
        # que só aparece em produção, semanas depois.
        hits.pop(ip, None)
        janela = hits[ip]
    if len(janela) >= limite:
        raise HTTPException(429, msg)
    janela.append(agora)


def _rate_limit(ip: str) -> None:
    _janela(_hits, ip, RATE_POR_IP, "Too many messages. Please wait a few minutes.")


def _sessao(sid: str | None, ip: str) -> tuple[str, Sessao]:
    _limpar()
    if sid and sid in _sessoes:
        return sid, _sessoes[sid]
    _janela(
        _novas,
        ip,
        NOVAS_SESSOES_POR_IP,
        "Too many new sessions from this address. Please wait a few minutes.",
    )
    novo = uuid.uuid4().hex
    _sessoes[novo] = Sessao(tenant_id=f"demo-{novo[:12]}")
    return novo, _sessoes[novo]


_VISTA_VALIDA = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _sincronizar_vista(sessao: Sessao, vista: str | None) -> bool:
    """Vista nova na mesma sessão começa conversa nova. Devolve se zerou.

    O "sim" cego: a conversa e as confirmações vivem aqui, presas ao cookie,
    enquanto o chat da página volta vazio depois do F5. Quem recarregava e
    digitava "sim" executava uma proposta que não estava mais na tela, e a
    outra aba do mesmo navegador confirmava a proposta que só esta mostrou.

    Zera a conversa, as confirmações pendentes e o custo mostrado. Mantém a
    agenda do ERP e o contador de escritas: o teto de 3 não reinicia com F5.
    O botão Reiniciar continua reiniciando tudo.

    Durante um turno em andamento não troca nada: zerar a conversa no meio do
    turno deixaria o turno escrevendo num histórico que ninguém mais lê. A
    próxima requisição desta vista, depois do turno, faz a troca.
    """
    if vista is None or not _VISTA_VALIDA.fullmatch(vista):
        return False
    if sessao.vista is None:
        sessao.vista = vista
        return False
    if vista == sessao.vista or sessao.lock.locked():
        return False
    sessao.vista = vista
    sessao.conversa = Conversation()
    if sessao.ctx is not None:
        sessao.ctx.confirmacoes = set()
        sessao.ctx.propostas_do_turno.clear()
        sessao.ctx.custo_micros = 0
    return True


ERP_INALCANCAVEL = "ERP unreachable."


def _contexto(s: Sessao) -> AgentContext:
    agora = datetime.now(timezone.utc)
    if s.ctx is None:
        s.ctx = AgentContext(
            conversation_id=s.tenant_id,
            tenant_id=s.tenant_id,
            policy=PolicyEngine(),
            erp=ErpAdapter(ERP_URL, tenant_id=s.tenant_id),
            at=agora,
        )
    else:
        s.ctx.at = agora  # o relógio é parâmetro, nunca lido dentro das regras
    return s.ctx


def _sse(tipo: str, dado: dict) -> str:
    return (
        f"event: {tipo}\ndata: {json.dumps(dado, ensure_ascii=False, default=str)}\n\n"
    )


@app.get("/health")
def health() -> dict[str, str | None]:
    # `status` é do processo, e continua 200 com o provedor fora: reiniciar o
    # container não conserta saldo nem credencial, e o healthcheck do compose
    # entraria em loop. `agente` diz como terminou a última chamada real ao
    # provedor, que é o que o "online" da tela nunca disse.
    # O motivo fino fica no log do servidor: numa rota aberta ele contaria a
    # qualquer um, por exemplo, que a conta do provedor ficou sem saldo.
    return {"status": "ok", "model": MODEL, "agente": agente.saude.resumo()["agente"]}


# `proof.json` é gerado no CI (scripts/gerar_provas.py) e baked na imagem. Fica
# em memória porque não muda em runtime: é uma foto do build.
_PROVAS: dict[str, object] | None = None
try:
    _PROVAS = json.loads((STATIC / "proof.json").read_text("utf-8"))
except (OSError, json.JSONDecodeError):
    # Sem o arquivo (rodando local, ou build sem o passo do CI) a barra some da
    # tela em vez de mostrar número inventado. O case study do portfólio dizia
    # "124 tests" enquanto a suíte rodava 134 — número de prova que envelhece em
    # silêncio é pior do que prova nenhuma.
    _PROVAS = None


@app.get("/api/meta")
def meta() -> dict[str, object]:
    """O que a barra de provas mostra. Nada aqui é digitado à mão.

    Só o que já é público na tela ou no repo. O saldo do teto NÃO entra: numa
    rota aberta ele diria a qualquer visitante quanto falta para derrubar a
    demo do dia (ver `test_health_nao_conta_quanto_sobrou_do_teto`).
    """
    return dict(_PROVAS) if _PROVAS else {}


@app.post("/api/chat")
async def chat(
    request: Request,
    response: Response,
    sid: Annotated[str | None, Cookie(alias="aurora_sid")] = None,
    x_real_ip: Annotated[str | None, Header()] = None,
    x_aurora_vista: Annotated[str | None, Header()] = None,
):
    corpo = await request.json()
    mensagem = str(corpo.get("mensagem", "")).strip()
    if not mensagem:
        raise HTTPException(400, "Empty message.")
    if len(mensagem) > MENSAGEM_MAX:
        raise HTTPException(400, f"Message too long (max {MENSAGEM_MAX} characters).")

    _rate_limit(_ip(x_real_ip))
    sid, sessao = _sessao(sid, _ip(x_real_ip))

    # `locked()` e `acquire()` sem await no meio: em asyncio nada interleva
    # entre os dois, então isto é atômico. Checar aqui e adquirir lá dentro do
    # gerador — que só começa a rodar depois — deixava duas requisições
    # simultâneas passarem pela checagem e consumirem o teto duas vezes.
    if sessao.lock.locked():
        raise HTTPException(409, "Still answering the previous one.")
    # Antes de pegar o lock: com ele preso, a sincronização não troca nada.
    _sincronizar_vista(sessao, x_aurora_vista)
    await sessao.lock.acquire()

    # Reserva antes da chamada paga, porque o custo real só existe depois dela.
    # A diferença volta na reconciliação, no `finally` abaixo.
    reserva = budget.RESERVA_POR_TURNO
    try:
        budget.reservar(reserva)
    except budget.Bloqueado as exc:
        sessao.lock.release()
        raise HTTPException(
            503,
            headers={"X-Aurora-Motivo": exc.motivo},
            detail={
                "kill_switch": "This demo is paused right now.",
                "teto_diario": "This demo hit today's usage cap. Please come back tomorrow.",
            }.get(exc.motivo, "This demo is unavailable right now."),
        ) from exc

    ctx = _contexto(sessao)
    ctx.idioma = "en" if corpo.get("idioma") == "en" else "pt"
    custo_antes = ctx.custo_micros

    async def stream():
        try:
            async for evento in agente.turno(sessao.conversa, mensagem, ctx):
                yield _sse(evento.tipo, evento.dado)
        except Exception:  # noqa: BLE001
            logging.getLogger("aurora.api").exception("turno_quebrou")
            yield _sse("error", {"codigo": "agent_internal_error"})
        finally:
            # Roda também quando o cliente desconecta no meio (GeneratorExit),
            # senão a sessão ficaria travada até o TTL.
            #
            # Reconciliar substitui o antigo "devolveu se não houve resposta".
            # Aquele era grosso nos dois sentidos: um turno que fez três
            # chamadas pagas e morreu no meio era cobrado como um só, e um turno
            # que não chegou a chamar nada devolvia exatamente uma unidade,
            # sempre a mesma, porque a unidade era turno. Agora o dia é debitado
            # pelo que o `usage` disse que aconteceu, inclusive quando é zero.
            budget.reconciliar(reserva, ctx.custo_micros - custo_antes)
            sessao.lock.release()
            yield _sse(
                "done",
                {
                    "estado": sessao.conversa.state.value,
                    "usd_conversa": round(ctx.custo_micros / 1_000_000, 6),
                },
            )

    resposta = StreamingResponse(stream(), media_type="text/event-stream")
    resposta.set_cookie(
        "aurora_sid",
        sid,
        max_age=SESSAO_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    resposta.headers["Cache-Control"] = "no-store"
    resposta.headers["X-Accel-Buffering"] = "no"
    return resposta


@app.get("/api/estado")
async def estado(
    response: Response,
    sid: Annotated[str | None, Cookie(alias="aurora_sid")] = None,
    x_real_ip: Annotated[str | None, Header()] = None,
    x_aurora_vista: Annotated[str | None, Header()] = None,
):
    """Espelho do ERP para o painel da direita. É o outro lado da integração."""
    sid, sessao = _sessao(sid, _ip(x_real_ip))
    _sincronizar_vista(sessao, x_aurora_vista)
    # ERP inalcançável (container parado, rede caída) respondia 500 cru, e a
    # tela mostrava erro de console em vez de "sem resposta".
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{ERP_URL}/admin/estado", headers={"X-Tenant-Id": sessao.tenant_id}
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, ERP_INALCANCAVEL) from exc
    dados = r.json()
    dados["conversa"] = {
        "estado": sessao.conversa.state.value,
        "escritas": sessao.ctx.escritas_realizadas if sessao.ctx else 0,
        "escalada": sessao.conversa.escalation_reason,
    }
    resposta = Response(
        json.dumps(dados, ensure_ascii=False), media_type="application/json"
    )
    resposta.set_cookie(
        "aurora_sid",
        sid,
        max_age=SESSAO_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resposta


@app.post("/api/chaos")
async def chaos(
    ligado: bool,
    sid: Annotated[str | None, Cookie(alias="aurora_sid")] = None,
    x_real_ip: Annotated[str | None, Header()] = None,
    x_aurora_vista: Annotated[str | None, Header()] = None,
):
    _rate_limit(_ip(x_real_ip))
    sid, sessao = _sessao(sid, _ip(x_real_ip))
    _sincronizar_vista(sessao, x_aurora_vista)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{ERP_URL}/admin/chaos",
                params={"ligado": ligado},
                headers={"X-Tenant-Id": sessao.tenant_id},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, ERP_INALCANCAVEL) from exc
    resposta = Response(r.text, media_type="application/json")
    resposta.set_cookie(
        "aurora_sid",
        sid,
        max_age=SESSAO_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resposta


@app.post("/api/reset")
async def reset(
    sid: Annotated[str | None, Cookie(alias="aurora_sid")] = None,
    x_real_ip: Annotated[str | None, Header()] = None,
    x_aurora_vista: Annotated[str | None, Header()] = None,
):
    _rate_limit(_ip(x_real_ip))
    sid, sessao = _sessao(sid, _ip(x_real_ip))
    _sincronizar_vista(sessao, x_aurora_vista)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{ERP_URL}/admin/reset", headers={"X-Tenant-Id": sessao.tenant_id}
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, ERP_INALCANCAVEL) from exc
    sessao.conversa = Conversation()
    sessao.ctx = None
    resposta = Response('{"ok":true}', media_type="application/json")
    resposta.set_cookie(
        "aurora_sid",
        sid,
        max_age=SESSAO_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resposta


# HEAD junto com GET: parte dos robôs de preview de link e dos monitores de
# uptime bate de HEAD primeiro, e `@app.get` sozinho devolvia 405 para eles.
@app.api_route("/", methods=["GET", "HEAD"])
def index() -> FileResponse:
    # Sem Cache-Control o navegador escolhe o prazo por heuristica e continua
    # servindo a versao anterior da demo depois de um deploy, sem revalidar.
    # `no-cache` nao proibe guardar: obriga a perguntar antes de usar, e a
    # resposta vira 304 quando nada mudou.
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=STATIC), name="static")
