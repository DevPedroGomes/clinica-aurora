"""Estado do ERP, isolado por tenant de sessão.

Cada visitante da demo recebe o próprio `tenant_id` e a própria agenda semeada.
Ninguém vê nem altera o dado de outro — é o mesmo padrão do voice_rag, e é o que
permite deixar a demo aberta sem login.

Estado em memória de propósito: nada aqui sobrevive à sessão (escopo §7).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count

from .models import Consulta, ConsultaStatus, Paciente, Profissional

TENANT_TTL = timedelta(hours=2)
MAX_TENANTS = 500

BAHIA = timezone(timedelta(hours=-3))

PROFISSIONAIS = [
    Profissional(id="prof_bruno", nome="Dr. Bruno Salles", especialidade="Clínico geral"),
    Profissional(id="prof_helena", nome="Dra. Helena Costa", especialidade="Dermatologia"),
]

PACIENTES = [
    Paciente(id="pac_ana", nome="Ana Ribeiro", telefone="(71) 9****-**32"),
    Paciente(id="pac_joao", nome="João Meireles", telefone="(71) 9****-**07"),
    Paciente(id="pac_marta", nome="Marta Lins", telefone="(71) 9****-**415"),
]


@dataclass
class Tenant:
    consultas: dict[str, Consulta] = field(default_factory=dict)
    criado_em: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    down: bool = False
    """Quando True o ERP responde 503 para este tenant. É o botão 'derrubar o sistema'."""


def _seed(agora: datetime, ids: count) -> dict[str, Consulta]:
    """Agenda inicial ancorada no relógio, não em datas fixas.

    A demo precisa que exista sempre: uma consulta longe (remarcável), uma dentro
    da janela de 24h (para provar a recusa por política) e um horário ocupado
    (para provar o conflito).
    """
    hoje = agora.astimezone(BAHIA)
    amanha = (hoje + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    daqui_2h = (hoje + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)

    linhas = [
        # longe o suficiente para ser remarcada sem cair na regra das 24h
        ("pac_ana", "prof_bruno", amanha, 45),
        # ocupa 11h de amanhã: qualquer remarcação para cá dá SLOT_CONFLICT
        ("pac_marta", "prof_bruno", amanha.replace(hour=11), 30),
        # daqui a 2h: cancelar isto exige motivo (CANCEL_WINDOW_REQUIRES_REASON)
        ("pac_joao", "prof_helena", daqui_2h, 30),
    ]

    consultas: dict[str, Consulta] = {}
    for paciente_id, profissional_id, inicio, duracao in linhas:
        cid = f"cons_{next(ids)}"
        consultas[cid] = Consulta(
            id=cid,
            paciente_id=paciente_id,
            profissional_id=profissional_id,
            inicio=inicio,
            fim=inicio + timedelta(minutes=duracao),
        )
    return consultas


class Store:
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._lock = threading.Lock()
        self._ids = count(8812)

    # -- ciclo de vida do tenant -------------------------------------------------

    def tenant(self, tenant_id: str, agora: datetime) -> Tenant:
        with self._lock:
            self._evict(agora)
            t = self._tenants.get(tenant_id)
            if t is None:
                if len(self._tenants) >= MAX_TENANTS:
                    # Despeja o mais antigo em vez de recusar: a demo nunca deve
                    # dizer "cheio" para um visitante novo.
                    mais_antigo = min(self._tenants, key=lambda k: self._tenants[k].criado_em)
                    del self._tenants[mais_antigo]
                t = Tenant(consultas=_seed(agora, self._ids))
                self._tenants[tenant_id] = t
            return t

    def _evict(self, agora: datetime) -> None:
        expirados = [k for k, v in self._tenants.items() if agora - v.criado_em > TENANT_TTL]
        for k in expirados:
            del self._tenants[k]

    def reset(self, tenant_id: str, agora: datetime) -> Tenant:
        with self._lock:
            t = Tenant(consultas=_seed(agora, self._ids))
            self._tenants[tenant_id] = t
            return t

    # -- consultas ---------------------------------------------------------------

    def listar(
        self, tenant: Tenant, profissional_id: str | None, data: datetime | None
    ) -> list[Consulta]:
        itens = [c for c in tenant.consultas.values() if c.status == ConsultaStatus.AGENDADA]
        if profissional_id:
            itens = [c for c in itens if c.profissional_id == profissional_id]
        if data:
            alvo = data.astimezone(BAHIA).date()
            itens = [c for c in itens if c.inicio.astimezone(BAHIA).date() == alvo]
        return sorted(itens, key=lambda c: c.inicio)

    def novo_id(self) -> str:
        return f"cons_{next(self._ids)}"


store = Store()
