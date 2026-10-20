"""Catálogo dos textos que o CÓDIGO escreve, por idioma da sessão.

Até 2026-10 só três frases de fallback seguiam o idioma da sessão. Todo o resto
que o código punha na conversa era português fixo: o prompt do sistema, o bloco
de contexto de cada turno, a ordem do gate de confirmação, as mensagens de erro
das tools e as dicas do motor de política. Numa sessão em inglês, a última coisa
que o modelo lia antes de redigir a confirmação era "Antes de executar: confirme
com a pessoa...", e às vezes ele respondia no idioma dessa instrução.

Regra: texto que o código produz vem daqui, com uma entrada por idioma. Uma
chave sem as duas traduções quebra `tests/test_i18n.py`, não a demo.

O idioma do que o MODELO escreve é decidido em `detector_idioma.py` e conferido
no loop do agente.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

Idioma = Literal["pt", "en"]
IDIOMAS: tuple[Idioma, ...] = ("pt", "en")


def normalizar(valor: object) -> Idioma:
    """Idioma da sessão a partir do que o navegador mandou. Português por padrão,
    que é o que a interface abre sem `?lang=en`."""
    return "en" if valor == "en" else "pt"


# Nome de cada idioma de RESPOSTA, escrito no idioma da sessão. A resposta pode
# sair em espanhol numa sessão em inglês (quem escreve em espanhol recebe
# espanhol), então esta tabela tem mais linhas do que o catálogo tem idiomas.
NOMES: dict[str, dict[Idioma, str]] = {
    "pt": {"pt": "português", "en": "Portuguese"},
    "en": {"pt": "inglês", "en": "English"},
    "es": {"pt": "espanhol", "en": "Spanish"},
}


def nome_do_idioma(idioma: str, na_lingua: str) -> str:
    sessao = normalizar(na_lingua)
    return NOMES.get(idioma, NOMES[sessao])[sessao]


CATALOGO: dict[str, dict[Idioma, str]] = {
    # -- frases que chegam à tela sem passar pelo modelo ----------------------
    # Nenhuma afirma que algo foi feito.
    "fala.confirmacao": {
        "pt": "Posso seguir?",
        "en": "Shall I go ahead?",
    },
    "fala.mudo": {
        "pt": "Desculpe, me perdi. Pode repetir?",
        "en": "Sorry, I lost track. Could you say that again?",
    },
    "fala.recusa": {
        "pt": "Não posso ajudar com isso. Só consigo cuidar da agenda da clínica.",
        "en": "I can't help with that. I can only handle the clinic's schedule.",
    },
    # -- notas e ordens que o código põe na conversa para o modelo ler --------
    "nota.escrita_sem_resposta": {
        "pt": (
            "(A alteração acima foi gravada no ERP. A resposta à pessoa não chegou "
            "a ser escrita porque o provedor falhou; a tela avisou que a gravação "
            "aconteceu.)"
        ),
        "en": (
            "(The change above was written to the ERP. The reply to the person was "
            "never written because the provider failed; the screen told them the "
            "write happened.)"
        ),
    },
    "gate.confirmar": {
        "pt": (
            "Antes de executar: confirme com a pessoa o que vai ser feito e aguarde "
            "a resposta dela. Não diga que o sistema vai confirmar outras alterações."
        ),
        "en": (
            "Before executing: confirm with the person what is about to be done "
            "and wait for their answer. Do not say the system will confirm other "
            "changes."
        ),
    },
    "erro.uma_por_vez": {
        "pt": "Uma alteração por vez. Diga que faz uma de cada vez e não prometa as outras.",
        "en": "One change at a time. Say you do one at a time and do not promise the others.",
    },
    "erro.tool_desconhecida": {
        "pt": "Não consigo fazer isso.",
        "en": "I can't do that.",
    },
    "erro.input_invalido": {
        "pt": "Não entendi os detalhes. Pode repetir?",
        "en": "I didn't get the details. Could you repeat them?",
    },
    "erro.teto_de_escritas": {
        "pt": (
            "Esta conversa atingiu o limite de alterações da demonstração e foi "
            "encerrada."
        ),
        "en": "This conversation hit the demo's change limit and was closed.",
    },
    "idioma.reescrever": {
        "pt": (
            "<verificacao_de_idioma>A sua última resposta não está em {idioma}, "
            "que é o idioma da resposta deste turno. Reescreva a mesma resposta, "
            "com o mesmo conteúdo, inteira em {idioma}. Não comente a troca."
            "</verificacao_de_idioma>"
        ),
        "en": (
            "<language_check>Your last reply is not in {idioma}, the reply "
            "language for this turn. Rewrite the same reply, with the same "
            "content, entirely in {idioma}. Do not mention the change."
            "</language_check>"
        ),
    },
    # -- bloco de contexto de cada turno (ver `agent._com_contexto`) ----------
    "contexto.formato_agora": {"pt": "%A, %d/%m/%Y %H:%M", "en": "%A, %Y-%m-%d %H:%M"},
    "contexto.bloco": {
        "pt": (
            "<contexto_operacional>\n"
            "agora: {agora}\n"
            "profissionais: prof_bruno (Dr. Bruno Salles) · prof_helena (Dra. Helena Costa)\n"
            "idioma da resposta: {idioma}. Escreva a resposta inteira em {idioma},\n"
            "  qualquer que seja o idioma dos resultados de ferramenta, dos dados da\n"
            "  agenda ou dos turnos anteriores.\n"
            "</contexto_operacional>\n"
        ),
        "en": (
            "<operational_context>\n"
            "now: {agora}\n"
            "practitioners: prof_bruno (Dr. Bruno Salles) · prof_helena (Dra. Helena Costa)\n"
            "reply language: {idioma}. Write the whole reply in {idioma}, whatever\n"
            "  the language of tool results, schedule data or earlier turns.\n"
            "</operational_context>\n"
        ),
    },
    "estado.aguardando_confirmacao": {
        "pt": "aguardando confirmação",
        "en": "waiting for confirmation",
    },
    # -- dados que as tools escrevem na saída ---------------------------------
    "agenda.outro_paciente": {"pt": "outro paciente", "en": "another patient"},
    "agenda.vaga_livre": {"pt": "vaga livre", "en": "free slot"},
    # -- erros das tools (ToolError.user_message) -----------------------------
    "tool.erp_indisponivel": {
        "pt": "Não consegui acessar a agenda agora. Nada foi alterado.",
        "en": "I couldn't reach the schedule right now. Nothing was changed.",
    },
    "tool.cadastro_recusado": {
        "pt": "Não consegui consultar o cadastro agora.",
        "en": "I couldn't look up the patient registry right now.",
    },
    "tool.paciente_nao_encontrado": {
        "pt": "Não encontrei esse nome no cadastro.",
        "en": "I couldn't find that name in the registry.",
    },
    "tool.paciente_ambiguo": {
        "pt": "Há mais de um paciente com esse nome. Peça o sobrenome.",
        "en": "More than one patient has that name. Ask for the surname.",
    },
    "tool.listar_recusado": {
        "pt": "Não consegui consultar essa agenda.",
        "en": "I couldn't look up that schedule.",
    },
    "tool.horario_invalido": {
        "pt": "Não entendi o horário. Pode repetir com o dia e a hora?",
        "en": (
            "I didn't understand the time. Could you repeat it with the day and "
            "time?"
        ),
    },
    "tool.nao_encontrada": {
        "pt": "Não encontrei essa consulta na agenda.",
        "en": "I couldn't find that appointment in the schedule.",
    },
    "tool.checagem_recusada": {
        "pt": "Não consegui consultar a agenda para checar o horário.",
        "en": "I couldn't check the schedule to verify that time.",
    },
    "tool.horario_indisponivel": {
        "pt": "Esse horário não está disponível.",
        "en": "That time is not available.",
    },
    "tool.remarcar_recusado": {
        "pt": "A agenda recusou essa alteração.",
        "en": "The schedule rejected that change.",
    },
    "tool.cancelar_exige_motivo": {
        "pt": "Para cancelar essa consulta eu preciso registrar um motivo.",
        "en": "To cancel this appointment I need to record a reason.",
    },
    "tool.cancelar_recusado": {
        "pt": "A agenda recusou o cancelamento.",
        "en": "The schedule rejected the cancellation.",
    },
    "tool.vaga_nao_permitida": {
        "pt": "Não posso abrir esse horário.",
        "en": "I can't open that time.",
    },
    "tool.vaga_recusada": {
        "pt": "A agenda recusou esse horário.",
        "en": "The schedule rejected that time.",
    },
    "tool.marcar_nao_permitido": {
        "pt": "Não posso marcar nesse horário.",
        "en": "I can't book at that time.",
    },
    "tool.marcar_recusado": {
        "pt": "A agenda recusou essa consulta.",
        "en": "The schedule rejected that appointment.",
    },
    # -- dicas de redação do motor de política (Decision.explanation_hint) ----
    # O motor devolve a CHAVE; o texto sai daqui no idioma da sessão. Nenhuma
    # dica diz a regra (o número de horas, o expediente): ver `decision.py`.
    "dica.pedir_motivo": {
        "pt": (
            "diga que, para cancelar em cima da hora, a regra da clínica pede um "
            "motivo, e pergunte qual é"
        ),
        "en": (
            "say that the clinic's rule asks for a reason to cancel at short "
            "notice, and ask what it is"
        ),
    },
    "dica.confirmar_cancelamento": {
        "pt": "confirme o cancelamento",
        "en": "confirm the cancellation",
    },
    "dica.horario_passou": {
        "pt": "diga que esse horário já passou e ofereça outro",
        "en": "say that this time has already passed and offer another",
    },
    "dica.dia_fechado": {
        "pt": "diga que a clínica não atende nesse dia e ofereça outro",
        "en": "say that the clinic is closed on that day and offer another",
    },
    "dica.fora_do_expediente": {
        "pt": "diga que o horário está fora do expediente e ofereça um dentro",
        "en": "say that the time is outside opening hours and offer one within them",
    },
    "dica.horario_ocupado": {
        "pt": "diga que o horário está ocupado e ofereça outro",
        "en": "say that the time is taken and offer another",
    },
    "dica.paciente_ja_tem_consulta": {
        "pt": (
            "diga que esse paciente já tem consulta nesse dia e ofereça outro dia, "
            "sem prometer as outras consultas pedidas"
        ),
        "en": (
            "say that this patient already has an appointment that day and offer "
            "another day, without promising the other appointments asked for"
        ),
    },
    "dica.confirmar_remarcacao": {
        "pt": "confirme a remarcação com o horário novo",
        "en": "confirm the reschedule with the new time",
    },
    "dica.confirmar_abertura": {
        "pt": "confirme a abertura do horário",
        "en": "confirm the new slot",
    },
    "dica.passar_para_recepcao": {
        "pt": "diga que precisa passar para a recepção para continuar",
        "en": "say that it has to go to the front desk to continue",
    },
    "dica.seguir": {
        "pt": "siga normalmente",
        "en": "carry on normally",
    },
}


def texto(chave: str, sessao: str, /, **campos: str) -> str:
    """O texto de `chave` no idioma da sessão, com `campos` preenchidos.

    Chave desconhecida é KeyError de propósito: um texto que falta é bug de
    quem escreveu o código, e o teste do catálogo pega antes do deploy.
    """
    entrada = CATALOGO[chave][normalizar(sessao)]
    return entrada.format(**campos) if campos else entrada


# -- schema das tools em inglês ---------------------------------------------
#
# A descrição em português continua morando no decorator `@tool`, que é a
# fonte que o servidor MCP publica. Aqui fica só a tradução: as tools são
# renderizadas ANTES do system no prefixo, e uma sessão em inglês com o schema
# em português mandava ao modelo mais um bloco grande de português a cada
# chamada. `tests/test_i18n.py` falha se uma tool ou campo novo vier sem
# tradução.
SCHEMA_EN: dict[str, dict[str, str]] = {
    "buscar_paciente": {
        "": (
            "Finds ONE registered patient by the name the person said (full name "
            "or first name, ignoring accents and case). Returns the id and the "
            "name, or says it found none or more than one. Never lists the "
            "registry."
        ),
        "nome": "Name the person said: full name or just the first name.",
    },
    "listar_agenda": {
        "": (
            "Lists booked appointments. With no filters it returns the clinic's "
            "whole schedule: use it that way when you don't know the practitioner "
            "or the day of the appointment you are looking for."
        ),
        "paciente_id": (
            "Patient the person named, with the id from buscar_paciente. Only "
            "their rows carry the name; the others say 'another patient'."
        ),
        "profissional_id": "Clinic practitioner. Omit it to see both schedules.",
        "data": "Day to look up, YYYY-MM-DD. Omit it to see the clinic's whole schedule.",
    },
    "remarcar_consulta": {
        "": "Reschedules an existing appointment to a new time.",
        "consulta_id": "Appointment id, taken from listar_agenda.",
        "novo_inicio": "New start, ISO 8601 with timezone.",
    },
    "cancelar_consulta": {
        "": "Cancels a booked appointment, recording the reason.",
        "motivo": "Cancellation reason, when the person gives one.",
    },
    "abrir_vaga": {
        "": "Opens a free slot in a practitioner's schedule.",
        "inicio": "Slot start, ISO 8601 with timezone.",
        "duracao_min": "Closed domain: 30, 45 or 60.",
    },
    "marcar_consulta": {
        "": (
            "Books an appointment for a registered patient with a practitioner. "
            "Use this when the person says WHO the appointment is for; use "
            "abrir_vaga when they only want to leave a free time in the schedule."
        ),
        "paciente_id": "Registered clinic patient, with the id from buscar_paciente.",
        "inicio": "Appointment start, ISO 8601 with timezone.",
        "duracao_min": "Closed domain: 30, 45 or 60.",
    },
}


def traduzir_tool(
    nome: str, descricao: str, schema: dict[str, Any], idioma: str
) -> tuple[str, dict[str, Any]]:
    """Descrição e schema da tool no idioma da sessão.

    Em português devolve o original. Em inglês troca a descrição da tool e a de
    cada campo que tiver tradução; o que faltar fica como está, porque derrubar
    a conversa por uma descrição seria pior do que mandá-la em português.
    """
    if normalizar(idioma) == "pt":
        return descricao, schema
    traducao = SCHEMA_EN.get(nome, {})
    novo = copy.deepcopy(schema)
    for campo, prop in novo.get("properties", {}).items():
        if campo in traducao and "description" in prop:
            prop["description"] = traducao[campo]
    return traducao.get("", descricao), novo
