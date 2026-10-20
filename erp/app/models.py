"""Modelos do clinica-aurora-erp.

Este serviço representa o SISTEMA DO CLIENTE, não o agente. Ele existe para que
a demo prove uma escrita real sobre HTTP contra um serviço separado, com estado
próprio e documentação pública — em vez de simular a integração dentro do agente.

Por isso ele não conhece nada de LLM, política ou conversa.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import AwareDatetime, BaseModel, Field


class ConsultaStatus(str, Enum):
    AGENDADA = "agendada"
    CANCELADA = "cancelada"


class Profissional(BaseModel):
    id: str = Field(examples=["prof_bruno"])
    nome: str = Field(examples=["Dr. Bruno Salles"])
    especialidade: str = Field(examples=["Clínico geral"])


class Paciente(BaseModel):
    id: str = Field(examples=["pac_ana"])
    nome: str = Field(examples=["Ana Ribeiro"])
    telefone: str = Field(
        examples=["+55 71 9****-**01"],
        description="Mascarado de propósito: a demo é pública e o agente nunca precisa do número inteiro.",
    )


class Consulta(BaseModel):
    id: str = Field(examples=["cons_8812"])
    paciente_id: str
    profissional_id: str
    inicio: datetime
    fim: datetime
    status: ConsultaStatus = ConsultaStatus.AGENDADA
    motivo_cancelamento: str | None = None


class ConsultaDetalhada(Consulta):
    """O que a listagem devolve: junta o nome para a tela, sem expor o registro inteiro."""

    paciente_nome: str
    profissional_nome: str


class RemarcarRequest(BaseModel):
    # AwareDatetime, não datetime: sem offset o Pydantic aceitava um naive, que
    # depois era comparado com a agenda semeada (aware) na checagem de conflito
    # e estourava TypeError — 500 cru na cara de quem está testando pelo /docs.
    # Com AwareDatetime a recusa vira um 422 explicando o que faltou.
    novo_inicio: AwareDatetime = Field(
        description="Início novo, ISO 8601 com timezone."
    )


class CancelarRequest(BaseModel):
    motivo: str = Field(min_length=1, max_length=200)


class AbrirVagaRequest(BaseModel):
    profissional_id: str
    # Mesmo motivo de RemarcarRequest.novo_inicio: naive aqui virava 500 na
    # comparação com a agenda semeada.
    inicio: AwareDatetime
    duracao_min: int = Field(description="30, 45 ou 60.")
    paciente_id: str = Field(
        default="",
        description=(
            "Paciente da consulta. Vazio cria um horário LIVRE (vaga), sem "
            "titular — é o mesmo endpoint porque, do lado do ERP, reservar o "
            "bloco de tempo é a mesma operação; o que muda é ter ou não dono."
        ),
        examples=["pac_ana"],
    )


class ErroResposta(BaseModel):
    detail: str
