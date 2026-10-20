"""clinica-aurora-erp — o "sistema do cliente" da demo.

API pequena de propósito: profissionais, pacientes e consultas. Ela é publicada
junto com a demo, com OpenAPI em /docs, para que o visitante possa inspecionar o
outro lado da integração e confirmar que a escrita aconteceu de verdade.

Duas coisas aqui existem só por causa da demo, e estão declaradas na página:
  - `X-Tenant-Id`: isola a agenda de cada visitante. O id e sempre emitido por
    um servidor, nunca escolhido por quem chama: o agente e o servidor MCP
    sorteiam o deles na rede interna, e quem explora o /docs pede um em
    `POST /sessions`. Sem header, ou com id que ninguem emitiu, a resposta e 401.
  - `/admin/chaos`: derruba o ERP sob demanda, para provar que o agente escala
    em vez de inventar uma confirmação quando a escrita falha.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from collections import defaultdict, deque

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyHeader

from .models import (
    AbrirVagaRequest,
    CancelarRequest,
    Consulta,
    ConsultaDetalhada,
    ConsultaStatus,
    Paciente,
    Profissional,
    RemarcarRequest,
)
from .store import (
    BAHIA,
    MAX_CONSULTAS_POR_TENANT,
    PACIENTES,
    PROFISSIONAIS,
    TENANT_TTL,
    Tenant,
    store,
)

DURACOES_VALIDAS = {30, 45, 60}

# Rate limit por IP nas rotas que mudam estado. Le o peer real que o Traefik
# poe em x-real-ip; quem chega sem o header cai num balde comum em vez de
# ganhar cota nova a cada requisicao.
ESCRITAS_POR_IP = 60
# Sessoes novas por IP, para quem chega de fora. Cada uma ocupa uma vaga da
# origem `api`; sem teto, um laco de POST /sessions despejaria as sessoes de
# quem esta explorando o /docs agora.
SESSOES_POR_IP = 10
JANELA_S = 600


class _TetoPorIp:
    def __init__(self, limite: int, mensagem: str) -> None:
        self.limite = limite
        self.mensagem = mensagem
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def __call__(self, request: Request) -> None:
        ip = request.headers.get("x-real-ip") or "sem-ip"
        agora = time.monotonic()
        janela = self.hits[ip]
        while janela and agora - janela[0] > JANELA_S:
            janela.popleft()
        if not janela:
            self.hits.pop(ip, None)  # nao deixa o dicionario crescer com IPs ociosos
            janela = self.hits[ip]
        if len(janela) >= self.limite:
            raise HTTPException(429, self.mensagem)
        janela.append(agora)


_teto_escritas = _TetoPorIp(
    ESCRITAS_POR_IP, "Too many changes. Please wait a few minutes."
)
_teto_sessoes = _TetoPorIp(
    SESSOES_POR_IP, "Too many sessions from this address. Please wait a few minutes."
)


# As dependencias sao FUNCOES de modulo, e nao as instancias acima. Com
# `from __future__ import annotations`, o FastAPI le `request: Request` como texto
# e o resolve pelos globais da funcao; o `__call__` de uma instancia nao traz esses
# globais, entao `Request` virava um parametro de query obrigatorio: toda escrita
# respondia 422 e o /openapi.json, 500 (27/09 a 14/10). Ver tests/test_erp_escrita.py.
def _limitar(request: Request) -> None:
    _teto_escritas(request)


def _limitar_sessoes(request: Request) -> None:
    _teto_sessoes(request)


def _interno(request: Request) -> bool:
    """Veio pela rede interna do Docker, e nao pela internet.

    ⚠️ O criterio e a AUSENCIA de `X-Real-Ip`, e ele so vale por duas coisas do
    deploy: a porta do ERP nao e publicada fora da rede `proxy`, e todo request
    publico passa pelo Traefik, que descarta o `X-Real-Ip` do cliente e escreve o
    do peer TCP real. Quem chega de fora sempre traz o header. Se um dia o ERP
    for exposto sem o Traefik na frente, este criterio deixa de valer."""
    return request.headers.get("x-real-ip") is None


app = FastAPI(
    title="Clínica Aurora ERP",
    version="1.0.0",
    description=(
        "Scheduling system of Clínica Aurora, a **fictional** clinic. "
        "Patients, practitioners and schedule are fictional.\n\n"
        "Published alongside the agent demo so anyone can see the other side of "
        "the integration: the agent talks to this API over HTTP, exactly as it "
        "would talk to a real client's ERP.\n\n"
        "Each demo session gets its own `X-Tenant-Id` and its own schedule.\n\n"
        "**To try it here:** call `POST /sessions`, then paste the `tenant_id` it "
        "returns into **Authorize** (the `X-Tenant-Id` header). Requests without "
        "it, or with an id that was not issued, get 401."
    ),
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ERP_CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


def agora() -> datetime:
    return datetime.now(timezone.utc)


_sessao_header = APIKeyHeader(
    name="X-Tenant-Id",
    auto_error=False,
    description="Demo session id, issued by POST /sessions.",
)

SEM_SESSAO = (
    "Missing X-Tenant-Id. Create a session with POST /sessions and send the "
    "tenant_id it returns in this header."
)
SESSAO_DESCONHECIDA = "Unknown or expired session. Create one with POST /sessions."


def resolver_tenant(
    request: Request,
    x_tenant_id: Annotated[str | None, Depends(_sessao_header)] = None,
) -> Tenant:
    """O tenant de quem chama, sem olhar se o modo caos esta ligado.

    Nao existe tenant padrao: antes, sem header, todo mundo caia no mesmo
    `demo`, e um visitante derrubava o ERP de outro. De fora, so vale id
    emitido por `POST /sessions`; um id da pagina ou do MCP (`demo-`, `mcp-`)
    recebe o mesmo 401 de um id inventado, sem dizer se ele existe. A rede
    interna pode semear `demo-` e `mcp-` na hora, que e o que mantem a sessao
    do visitante de pe depois de um restart do ERP."""
    if not x_tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, SEM_SESSAO)
    tenant_id = x_tenant_id[:64]
    origem = store.origem(tenant_id)
    interno = _interno(request)
    if origem is None or (not interno and origem != "api"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, SESSAO_DESCONHECIDA)
    t = store.tenant(tenant_id, agora(), criar=interno and origem != "api")
    if t is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, SESSAO_DESCONHECIDA)
    return t


SessaoDep = Annotated[Tenant, Depends(resolver_tenant)]


def get_tenant(t: SessaoDep) -> Tenant:
    if t.down:
        # 503 e não 500: é indisponibilidade declarada, e é o que o adapter do
        # agente trata como "sistema fora do ar" para escalar.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERP unavailable (chaos mode is on for this session).",
        )
    return t


TenantDep = Annotated[Tenant, Depends(get_tenant)]


def _detalhar(c: Consulta) -> ConsultaDetalhada:
    pac = next((p for p in PACIENTES if p.id == c.paciente_id), None)
    prof = next((p for p in PROFISSIONAIS if p.id == c.profissional_id), None)
    # Sem paciente devolve string VAZIA, e não "-". São coisas diferentes: vazio
    # é uma vaga livre, que não tem titular por definição; "-" é um id que
    # aponta para um cadastro que não existe, e isso é defeito de dado. Colapsar
    # os dois fazia a vaga aberta aparecer no painel como uma consulta cujo
    # nome do paciente sumiu.
    return ConsultaDetalhada(
        **c.model_dump(),
        paciente_nome=(pac.nome if pac else "-") if c.paciente_id else "",
        profissional_nome=prof.nome if prof else "-",
    )


# A raiz existe porque o dominio e publico: a demo linka /docs, mas quem digita
# o dominio nu caia num {"detail":"Not Found"} cru do FastAPI — pessimo cartao de
# visita para o "servico separado que voce pode inspecionar".
@app.get("/", include_in_schema=False)
def raiz() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/profissionais", response_model=list[Profissional], tags=["records"])
def listar_profissionais() -> list[Profissional]:
    return PROFISSIONAIS


@app.get("/pacientes", response_model=list[Paciente], tags=["records"])
def listar_pacientes(tenant: TenantDep) -> list[Paciente]:
    """The patient registry, with phones masked. Needs a session, like the schedule.

    `/profissionais` stays public: it is the clinic's storefront and identifies
    no patient.
    """
    return PACIENTES


@app.get("/consultas", response_model=list[ConsultaDetalhada], tags=["schedule"])
def listar_consultas(
    tenant: TenantDep,
    profissional_id: Annotated[str | None, Query()] = None,
    data: Annotated[
        datetime | None, Query(description="Filter by day (America/Bahia).")
    ] = None,
) -> list[ConsultaDetalhada]:
    return [_detalhar(c) for c in store.listar(tenant, profissional_id, data)]


@app.get(
    "/consultas/{consulta_id}",
    response_model=ConsultaDetalhada,
    tags=["schedule"],
    responses={404: {"description": "No such appointment in this session."}},
)
def obter_consulta(consulta_id: str, tenant: TenantDep) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None:
        # Id de outro tenant cai aqui: 404, nunca o dado do vizinho.
        raise HTTPException(status_code=404, detail="Appointment not found.")
    return _detalhar(c)


@app.patch(
    "/consultas/{consulta_id}",
    response_model=ConsultaDetalhada,
    tags=["schedule"],
    responses={
        404: {"description": "No such appointment in this session."},
        409: {"description": "Slot already taken."},
    },
)
def remarcar_consulta(
    consulta_id: str,
    body: RemarcarRequest,
    tenant: TenantDep,
    _: None = Depends(_limitar),
) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None or c.status is ConsultaStatus.CANCELADA:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    duracao = c.fim - c.inicio
    novo_inicio = body.novo_inicio
    novo_fim = novo_inicio + duracao

    # O ERP também recusa conflito. A política do agente é a primeira barreira,
    # esta é a segunda: um sistema real não confia em quem chama.
    for outra in tenant.consultas.values():
        if outra.id == c.id or outra.status is ConsultaStatus.CANCELADA:
            continue
        if outra.profissional_id != c.profissional_id:
            continue
        if novo_inicio < outra.fim and outra.inicio < novo_fim:
            raise HTTPException(
                status_code=409, detail="Slot already taken for this practitioner."
            )

    c.inicio = novo_inicio
    c.fim = novo_fim
    return _detalhar(c)


@app.post(
    "/consultas/{consulta_id}/cancelamento",
    response_model=ConsultaDetalhada,
    tags=["schedule"],
    responses={404: {"description": "No such appointment in this session."}},
)
def cancelar_consulta(
    consulta_id: str,
    body: CancelarRequest,
    tenant: TenantDep,
    _: None = Depends(_limitar),
) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    c.status = ConsultaStatus.CANCELADA
    c.motivo_cancelamento = body.motivo
    return _detalhar(c)


@app.post(
    "/consultas",
    response_model=ConsultaDetalhada,
    status_code=201,
    tags=["schedule"],
    responses={
        409: {"description": "Slot already taken."},
        422: {"description": "Invalid duration."},
    },
)
def abrir_vaga(
    body: AbrirVagaRequest, tenant: TenantDep, _: None = Depends(_limitar)
) -> ConsultaDetalhada:
    if len(tenant.consultas) >= MAX_CONSULTAS_POR_TENANT:
        raise HTTPException(
            status_code=429,
            detail="This demo schedule hit its appointment limit.",
        )
    if body.duracao_min not in DURACOES_VALIDAS:
        raise HTTPException(
            status_code=422, detail="Duration must be 30, 45 or 60 minutes."
        )
    if not any(p.id == body.profissional_id for p in PROFISSIONAIS):
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    # Vazio é válido (vaga sem titular). Preenchido tem que existir: aceitar um
    # id qualquer criaria uma consulta apontando para um paciente inventado, que
    # é exatamente o tipo de escrita que esta demo existe para tornar impossível.
    if body.paciente_id and not any(p.id == body.paciente_id for p in PACIENTES):
        raise HTTPException(status_code=404, detail="Patient not found.")

    fim = body.inicio + timedelta(minutes=body.duracao_min)
    for outra in tenant.consultas.values():
        if (
            outra.status is ConsultaStatus.CANCELADA
            or outra.profissional_id != body.profissional_id
        ):
            continue
        if body.inicio < outra.fim and outra.inicio < fim:
            raise HTTPException(
                status_code=409, detail="Slot already taken for this practitioner."
            )

    cid = store.novo_id()
    consulta = Consulta(
        id=cid,
        paciente_id=body.paciente_id,
        profissional_id=body.profissional_id,
        inicio=body.inicio,
        fim=fim,
    )
    tenant.consultas[cid] = consulta
    return _detalhar(consulta)


# -- superfície da demo, não do ERP ------------------------------------------


@app.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    tags=["demo"],
    summary="Create a demo session with its own schedule",
)
def criar_sessao(request: Request) -> dict[str, object]:
    """Issues a random `X-Tenant-Id` with a freshly seeded schedule.

    Send it in the `X-Tenant-Id` header (or paste it into **Authorize**) on every
    other call. It expires after two hours.
    """
    if not _interno(request):
        _limitar_sessoes(request)
    return {
        "tenant_id": store.emitir("api", agora()),
        "header": "X-Tenant-Id",
        "expires_in_seconds": int(TENANT_TTL.total_seconds()),
    }


# As rotas /admin servem a página e o MCP, e continuam atendendo com sessão.
# Saem da documentação pública: não são parte da integração que a demo mostra.
@app.post(
    "/admin/chaos",
    tags=["demo"],
    summary="Turn chaos mode on or off for this session",
    include_in_schema=False,
)
def chaos(ligado: bool, t: SessaoDep) -> dict[str, bool]:
    """With chaos on, every schedule endpoint answers 503 for this session.

    It exists so the visitor can watch the agent fail honestly: it says it could
    not write and escalates, instead of returning a made-up confirmation.
    """
    t.down = ligado
    return {"down": t.down}


@app.post(
    "/admin/reset",
    tags=["demo"],
    summary="Rebuild this session's initial schedule",
    include_in_schema=False,
)
def reset(t: SessaoDep) -> Response:
    store.resemear(t, agora())
    return Response(status_code=204)


@app.get(
    "/admin/estado",
    tags=["demo"],
    summary="This session's full schedule, for the panel",
    include_in_schema=False,
)
def estado(t: SessaoDep) -> dict[str, object]:
    consultas = sorted(t.consultas.values(), key=lambda c: c.inicio)
    return {
        "down": t.down,
        "timezone": "America/Bahia",
        "agora": agora().astimezone(BAHIA).isoformat(),
        "consultas": [_detalhar(c).model_dump(mode="json") for c in consultas],
    }
