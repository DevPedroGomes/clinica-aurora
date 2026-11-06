"""A página da demo, lida como texto: ela não tem suíte JS.

O que é pino aqui é o que já deu errado na tela: a página se
apresentava como agenda "de verdade", a versão em inglês mantinha a saudação e
o rótulo "ASSISTENTE" em português, o F5 deixava a proposta pendente viva, a
linha cancelada só era tachada (nada para leitor de tela), e o texto acessível
do registro colava a versão no número ("3.435tarifas").
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
html = (STATIC / "index.html").read_text("utf-8")
head = html.split("</head>")[0]


def test_titulo_e_descricao_dizem_que_e_ficticio():
    assert "fictícia" in head and "fictícios" in head


def test_nenhum_texto_chama_a_agenda_de_verdade():
    assert "agenda de verdade" not in html and "real schedule" not in html


def test_rotulos_da_conversa_sao_traduziveis():
    assert 'dataset.t = quem === "eu" ? "voce" : "assistente"' in html


def test_toda_chamada_a_api_leva_a_vista():
    assert html.count("X-Aurora-Vista") >= 4


def test_cancelada_tem_texto_para_leitor_de_tela():
    assert "aria-label" in html[html.index('c.status === "cancelada"') :][:400]


def test_saudacao_diz_que_a_clinica_e_ficticia_nos_dois_idiomas():
    assert "Clínica Aurora, uma clínica fictícia" in html
    assert "Clínica Aurora, a fictional clinic" in html


def test_texto_do_cenario_t5_nos_dois_idiomas():
    assert (
        "Agenda com escrita em sistema: um agente que marca, remarca e cancela numa "
        "agenda e só grava depois da confirmação. O cenário é uma clínica fictícia; "
        "os pacientes e o sistema de agenda também são fictícios."
    ) in html
    assert (
        "Scheduling that writes to a system: an agent that books, reschedules and "
        "cancels appointments and only writes after you confirm. The setting is a "
        "fictional clinic; the patients and the scheduling system are fictional too."
    ) in html


def test_proposta_pendente_e_montada_pela_pagina():
    """O "aguardando o seu sim" sai dos dados da proposta, não do modelo."""
    assert "Aguardando o seu sim para:" in html
    assert "Waiting for your yes to:" in html
    assert html.count("    acoes: {") == 2


def test_codigos_de_regra_tem_legenda_nos_dois_idiomas():
    for codigo in (
        "cancel_window_requires_reason",
        "outside_business_hours",
        "slot_conflict",
        "slot_in_the_past",
        "patient_daily_limit",
        "write_quota_exceeded",
    ):
        assert html.count(f"{codigo}:") == 2, codigo


def test_versao_no_registro_tem_separador():
    assert '<span class="versao"> · ' in html


def test_outros_projetos_leva_as_demonstracoes():
    assert 'href="https://gomio.com.br/demonstracoes"' in html


def test_comentarios_e_css_nao_citam_o_dominio_do_site():
    # o modo vitrine tira os comentários HTML, mas o CSS vai junto para a página
    estilo = html[html.index("<style>") : html.index("</style>")]
    assert "gomio.com.br" not in estilo
