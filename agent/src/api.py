"""A superfície pública da demo.

Sessão anônima com tenant isolado (padrão do voice_rag): cada visitante recebe
a própria agenda semeada e nunca vê a de outro. Sem login — cadastro afasta
visitante e não prova nada.
"""

from __future__ import annotations

import asyncio
import json
import os
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
JANELA_RATE = 600

STATIC = Path(__file__).resolve().parent.parent / "static"

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


_sessoes: dict[str, Sessao] = {}
_hits: dict[str, deque[float]] = defaultdict(deque)


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


def _rate_limit(ip: str) -> None:
    agora = time.monotonic()
    janela = _hits[ip]
    while janela and agora - janela[0] > JANELA_RATE:
        janela.popleft()
    if not janela:
        # Sem isto o dicionário nunca encolhe: cada IP que passou por aqui uma
        # vez deixa uma chave viva para sempre. É vazamento lento, mas é o tipo
        # que só aparece em produção, semanas depois.
        _hits.pop(ip, None)
        janela = _hits[ip]
    if len(janela) >= RATE_POR_IP:
        raise HTTPException(429, "Too many messages. Please wait a few minutes.")
    janela.append(agora)


def _sessao(sid: str | None) -> tuple[str, Sessao]:
    _limpar()
    if sid and sid in _sessoes:
        return sid, _sessoes[sid]
    novo = uuid.uuid4().hex
    _sessoes[novo] = Sessao(tenant_id=f"demo-{novo[:12]}")
    return novo, _sessoes[novo]


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
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL}


@app.post("/api/chat")
async def chat(
    request: Request,
    response: Response,
    sid: Annotated[str | None, Cookie(alias="aurora_sid")] = None,
    x_real_ip: Annotated[str | None, Header()] = None,
):
    corpo = await request.json()
    mensagem = str(corpo.get("mensagem", "")).strip()
    if not mensagem:
        raise HTTPException(400, "Empty message.")
    if len(mensagem) > MENSAGEM_MAX:
        raise HTTPException(400, f"Message too long (max {MENSAGEM_MAX} characters).")

    _rate_limit(_ip(x_real_ip))
    sid, sessao = _sessao(sid)

    # `locked()` e `acquire()` sem await no meio: em asyncio nada interleva
    # entre os dois, então isto é atômico. Checar aqui e adquirir lá dentro do
    # gerador — que só começa a rodar depois — deixava duas requisições
    # simultâneas passarem pela checagem e consumirem o teto duas vezes.
    if sessao.lock.locked():
        raise HTTPException(409, "Still answering the previous one.")
    await sessao.lock.acquire()

    try:
        budget.consumir()
    except budget.Bloqueado as exc:
        sessao.lock.release()
        raise HTTPException(
            503,
            {
                "kill_switch": "This demo is paused right now.",
                "teto_diario": "This demo hit today's usage cap. Please come back tomorrow.",
            }.get(exc.motivo, "This demo is unavailable right now."),
        ) from exc

    ctx = _contexto(sessao)

    async def stream():
        houve_resposta = False
        try:
            async for evento in agente.turno(sessao.conversa, mensagem, ctx):
                houve_resposta = True
                yield _sse(evento.tipo, evento.dado)
        except Exception:  # noqa: BLE001
            yield _sse("error", {"mensagem": "Something broke on my side."})
        finally:
            # Roda também quando o cliente desconecta no meio (GeneratorExit),
            # senão a sessão ficaria travada até o TTL.
            if not houve_resposta:
                budget.devolver()
            sessao.lock.release()
            yield _sse("done", {"estado": sessao.conversa.state.value})

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
):
    """Espelho do ERP para o painel da direita. É o outro lado da integração."""
    sid, sessao = _sessao(sid)
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            f"{ERP_URL}/admin/estado", headers={"X-Tenant-Id": sessao.tenant_id}
        )
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
):
    _rate_limit(_ip(x_real_ip))
    sid, sessao = _sessao(sid)
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(
            f"{ERP_URL}/admin/chaos",
            params={"ligado": ligado},
            headers={"X-Tenant-Id": sessao.tenant_id},
        )
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
):
    _rate_limit(_ip(x_real_ip))
    sid, sessao = _sessao(sid)
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{ERP_URL}/admin/reset", headers={"X-Tenant-Id": sessao.tenant_id}
        )
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
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
