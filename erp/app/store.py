"""Estado do ERP, isolado por tenant de sessão.

Cada visitante da demo recebe o próprio `tenant_id` e a própria agenda semeada.
Ninguém vê nem altera o dado de outro — é o mesmo padrão do voice_rag, e é o que
permite deixar a demo aberta sem login.

Estado em memória de propósito: nada aqui sobrevive à sessão (escopo §7).
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count

from .models import Consulta, ConsultaStatus, Paciente, Profissional

TENANT_TTL = timedelta(hours=2)

# Teto POR ORIGEM, e nao um teto so. Com um teto unico e despejo do mais antigo,
# 500 ids inventados bastavam para despejar a agenda de todo visitante da demo
# web: a sessao dele voltava semeada do zero no meio da conversa. Cada origem
# agora so despeja a si mesma.
#   demo: sessoes da pagina, criadas pelo agente (rede interna)
#   mcp:  sessoes do servidor MCP (rede interna)
#   api:  sessoes emitidas por POST /sessions, para quem explora o /docs
MAX_TENANTS_POR_ORIGEM = {"demo": 500, "mcp": 200, "api": 200}

# O ERP e publico de proposito — a demo perde o sentido se o visitante nao
# puder inspecionar o outro lado. Mas publico sem teto e outra coisa: sem isto,
# um laco de POST /consultas cresce a memoria do processo sem limite nenhum, e
# a VPS e compartilhada com todas as outras demos. O agente ja tem teto (P-5,
# 3 escritas por conversa); o ERP precisa do seu, porque ninguem e obrigado a
# passar pelo agente para falar com ele.
MAX_CONSULTAS_POR_TENANT = 40

BAHIA = timezone(timedelta(hours=-3))

# 5 = sábado (a clínica fecha ao meio-dia), 6 = domingo (não abre). Nenhum dos
# dois comporta um dia de agenda inteiro, então a semeadura os pula.
FIM_DE_SEMANA = {5, 6}

# Expediente por dia da semana (0 = segunda), em horas inteiras: abre, fecha.
# Mesma tabela de `agent/src/policy/rules.yaml`; `tests/test_seed.py` confere
# cada consulta semeada contra o motor de política, então as duas não divergem
# sem o teste acusar.
EXPEDIENTE = {0: (8, 18), 1: (8, 18), 2: (8, 18), 3: (8, 18), 4: (8, 18), 5: (8, 12)}

# A consulta do João nasce pelo menos isto à frente do relógio, para ainda
# estar no futuro quando a pessoa pedir para cancelar.
FOLGA_DO_JOAO = timedelta(hours=1)
DURACAO_DO_JOAO = timedelta(minutes=30)

PROFISSIONAIS = [
    Profissional(
        id="prof_bruno", nome="Dr. Bruno Salles", especialidade="Clínico geral"
    ),
    Profissional(
        id="prof_helena", nome="Dra. Helena Costa", especialidade="Dermatologia"
    ),
]


def mascarar_telefone(numero: str) -> str:
    """`+55 71 90000-0001` vira `+55 71 9****-**01`.

    Mantém o país, o DDD e o nono dígito, troca o resto por `*` e preserva os
    dois finais. Uma regra só, porque a máscara antiga era escrita à mão em cada
    linha e uma delas saiu com um dígito a mais.
    """
    prefixo, resto = numero[:8], numero[8:]
    total = sum(ch.isdigit() for ch in resto)
    saida: list[str] = []
    vistos = 0
    for ch in resto:
        if ch.isdigit():
            vistos += 1
            saida.append(ch if vistos > total - 2 else "*")
        else:
            saida.append(ch)
    return prefixo + "".join(saida)


# Números claramente fictícios (+55 71 90000-00NN), e mesmo assim só saem
# mascarados: o ERP é público, e o agente nunca precisa do número inteiro.
PACIENTES = [
    Paciente(
        id="pac_ana",
        nome="Ana Ribeiro",
        telefone=mascarar_telefone("+55 71 90000-0001"),
    ),
    Paciente(
        id="pac_joao",
        nome="João Meireles",
        telefone=mascarar_telefone("+55 71 90000-0002"),
    ),
    Paciente(
        id="pac_marta",
        nome="Marta Lins",
        telefone=mascarar_telefone("+55 71 90000-0003"),
    ),
]


@dataclass
class Tenant:
    consultas: dict[str, Consulta] = field(default_factory=dict)
    criado_em: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    down: bool = False
    """Quando True o ERP responde 503 para este tenant. É o botão 'derrubar o sistema'."""


def _proximo_dia_util(hoje: datetime) -> datetime:
    """Primeiro dia de expediente INTEIRO depois de hoje, às 9h.

    "Amanhã" não serve de âncora, e isso não era teórico: num sábado a agenda
    inteira nascia num domingo, dia em que a clínica não abre, e o preset do
    caminho feliz — remarcar para as 16h — era RECUSADO por outside_business_hours.
    Numa sexta dava no mesmo, porque sábado fecha ao meio-dia. Dois dias em sete
    a demo abria provando o contrário do que se propõe a provar.

    Segunda a sexta é o único intervalo em que 08:00–18:00 vale por completo, e
    é por isso que a busca pula o fim de semana.

    Nota de acoplamento: o expediente vive em `policy/rules.yaml`, do lado do
    agente. Aqui a clínica só precisa saber quando ela mesma abre — um ERP real
    sabe —, mas os dois têm que contar a mesma história. Mudou o expediente lá,
    revise `FIM_DE_SEMANA` aqui.
    """
    dia = hoje + timedelta(days=1)
    while dia.weekday() in FIM_DE_SEMANA:
        dia += timedelta(days=1)
    return dia.replace(hour=9, minute=0, second=0, microsecond=0)


def _cabe_no_expediente(inicio: datetime, duracao: timedelta) -> bool:
    horas = EXPEDIENTE.get(inicio.weekday())
    if horas is None:
        return False
    abre = inicio.replace(hour=horas[0], minute=0, second=0, microsecond=0)
    fecha = inicio.replace(hour=horas[1], minute=0, second=0, microsecond=0)
    return abre <= inicio and inicio + duracao <= fecha


def _horario_do_joao(hoje: datetime) -> datetime:
    """O primeiro meio-hora de expediente a partir de `hoje + FOLGA_DO_JOAO`.

    Antes era "daqui a 2 horas", em qualquer horário: às 18h56 de uma terça a
    agenda abria com o João às 20h, fora do expediente que a própria regra
    impõe. Agora a consulta nasce sempre dentro do expediente, e quase sempre a
    menos de 24 h, que é o que faz "Cancele a consulta do João" pedir o motivo
    (P-1). A exceção vai de sábado depois das 10h30 a domingo 8h: nela nenhum
    horário de expediente fica a menos de 24 h (o próximo é segunda às 8h), e
    entre a regra de expediente e a de cancelamento, a semeadura respeita as
    duas: o João vai para segunda às 8h e o cancelamento só pede o "sim".
    """
    passo = timedelta(minutes=30)
    inicio = (hoje + FOLGA_DO_JOAO).replace(second=0, microsecond=0)
    if inicio.minute % 30:
        inicio += timedelta(minutes=30 - inicio.minute % 30)
    while not _cabe_no_expediente(inicio, DURACAO_DO_JOAO):
        inicio += passo
    return inicio


def _seed(agora: datetime, ids: count) -> dict[str, Consulta]:
    """Agenda inicial ancorada no relógio, não em datas fixas.

    Toda consulta semeada respeita o expediente, e a agenda permite os roteiros
    que a página e a verificação pós-deploy usam:

    - Ana: remarcar para as 16h passa (dia útil inteiro);
    - Marta: ocupa as 11h do mesmo dia e profissional da Ana, para o conflito
      (P-3) existir; e não tem consulta AMANHÃ, para "marque 10 consultas para
      a Marta amanhã" render uma proposta e a segunda do dia cair na P-6;
    - João: a menos de 24 h sempre que o expediente permite, para o
      cancelamento pedir o motivo (P-1). Ver `_horario_do_joao`.

    Por isso Ana e Marta nascem no primeiro dia útil inteiro DEPOIS de amanhã:
    no próximo dia útil, de segunda a quinta, a Marta estaria amanhã.
    """
    hoje = agora.astimezone(BAHIA)
    util = _proximo_dia_util(hoje + timedelta(days=1))

    linhas = [
        # Dia útil inteiro: remarcar para as 16h cabe no expediente em qualquer
        # dia da semana em que a demo for aberta.
        ("pac_ana", "prof_bruno", util, 45),
        # ocupa 11h do mesmo dia: qualquer remarcação para cá dá SLOT_CONFLICT
        ("pac_marta", "prof_bruno", util.replace(hour=11), 30),
        # cancelar isto exige motivo (CANCEL_WINDOW_REQUIRES_REASON)
        ("pac_joao", "prof_helena", _horario_do_joao(hoje), 30),
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

    @staticmethod
    def origem(tenant_id: str) -> str | None:
        """A origem declarada no prefixo do id, ou None se nao for uma conhecida."""
        prefixo, _, resto = tenant_id.partition("-")
        return prefixo if resto and prefixo in MAX_TENANTS_POR_ORIGEM else None

    def tenant(self, tenant_id: str, agora: datetime, *, criar: bool) -> Tenant | None:
        """O tenant do id, ou None se ele nao existe e `criar` e falso.

        Quem decide se o id pode nascer aqui e a rota: so a rede interna semeia
        id na hora. Id vindo de fora so vale se foi emitido por `emitir`."""
        with self._lock:
            self._evict(agora)
            t = self._tenants.get(tenant_id)
            if t is None and criar:
                t = self._criar(tenant_id, agora)
            return t

    def emitir(self, origem: str, agora: datetime) -> str:
        """Cria um tenant com id sorteado aqui, 128 bits, que ninguem escolhe."""
        with self._lock:
            self._evict(agora)
            tenant_id = f"{origem}-{secrets.token_hex(16)}"
            self._criar(tenant_id, agora)
            return tenant_id

    def _criar(self, tenant_id: str, agora: datetime) -> Tenant:
        origem = self.origem(tenant_id)
        if origem is None:
            raise ValueError(f"origem desconhecida no id {tenant_id!r}")
        da_origem = [k for k in self._tenants if k.startswith(f"{origem}-")]
        if len(da_origem) >= MAX_TENANTS_POR_ORIGEM[origem]:
            # Despeja o mais antigo DA MESMA ORIGEM em vez de recusar: a demo
            # nunca deve dizer "cheio" para um visitante novo, e encher uma
            # origem nao pode apagar a sessao de quem esta em outra.
            mais_antigo = min(da_origem, key=lambda k: self._tenants[k].criado_em)
            del self._tenants[mais_antigo]
        t = Tenant(consultas=_seed(agora, self._ids), criado_em=agora)
        self._tenants[tenant_id] = t
        return t

    def _evict(self, agora: datetime) -> None:
        expirados = [
            k for k, v in self._tenants.items() if agora - v.criado_em > TENANT_TTL
        ]
        for k in expirados:
            del self._tenants[k]

    def resemear(self, tenant: Tenant, agora: datetime) -> None:
        """Volta a agenda deste tenant ao inicio, e religa o ERP para ele."""
        with self._lock:
            tenant.consultas = _seed(agora, self._ids)
            tenant.down = False
            tenant.criado_em = agora

    # -- consultas ---------------------------------------------------------------

    def listar(
        self, tenant: Tenant, profissional_id: str | None, data: datetime | None
    ) -> list[Consulta]:
        itens = [
            c for c in tenant.consultas.values() if c.status == ConsultaStatus.AGENDADA
        ]
        if profissional_id:
            itens = [c for c in itens if c.profissional_id == profissional_id]
        if data:
            alvo = data.astimezone(BAHIA).date()
            itens = [c for c in itens if c.inicio.astimezone(BAHIA).date() == alvo]
        return sorted(itens, key=lambda c: c.inicio)

    def novo_id(self) -> str:
        return f"cons_{next(self._ids)}"


store = Store()
