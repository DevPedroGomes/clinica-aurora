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
    # o favicon em data URI também tem <style>: a busca é pela folha real, que começa em nova linha
    estilo = html[html.index("<style>\n") : html.rindex("</style>")]
    assert ":root{" in estilo
    assert "gomio.com.br" not in estilo


# -- marca v1.3 e textos --------------------------------------------------------

# Trechos do desenho em marca/masters/svg/web/: o símbolo do logo de cabeçalho, o
# início do wordmark e o favicon. O arquivo mestre mora fora deste repositório.
SIMBOLO_LOGO = 'd="M136 34L68 0L0 34L0 102L68 136L126.29 106.86L58.29 72.86'
WORDMARK_LOGO = 'd="M210.48 120.43Q204.16 120.43 198.2 118.99'
FAVICON = (
    "M28 7L14 0L0 7L0 21L14 28L26 22L12 15L12 13L14 12L24 17L24 19L28 21L28 15L14 8"
)

RAIZ = STATIC.parent.parent
og = (RAIZ / "scripts" / "og" / "og.html").read_text("utf-8")
captura = (RAIZ / "scripts" / "og" / "captura.py").read_text("utf-8")


def _link_da_marca() -> str:
    inicio = html.index('<a class="marca"')
    return html[inicio : html.index("</a>", inicio)]


def test_cabecalho_usa_o_logo_novo_em_linha():
    marca = _link_da_marca()
    assert 'viewBox="0 0 489.48 136"' in marca
    assert SIMBOLO_LOGO in marca and WORDMARK_LOGO in marca
    assert 'aria-label="Gomio"' in marca


def test_logo_tem_cor_unica_vinda_da_pagina():
    marca = _link_da_marca()
    assert marca.count("fill=") == 1 and 'fill="currentColor"' in marca
    assert "--marca:#f4f2ed" in html
    assert "color:var(--marca)" in html
    assert ".marca svg{display:block;width:124px" in html


def test_logo_antigo_saiu():
    for pagina in (html, og):
        assert 'class="o"' not in pagina and ".marca .o" not in pagina


def test_favicon_e_o_novo_e_nao_pede_nada_a_outro_dominio():
    icone = head[head.index('<link rel="icon"') :].split(">\n")[0]
    assert icone.count("data:image/svg+xml,") == 1
    assert "viewBox='-2 -2 32 32'" in icone and FAVICON in icone
    assert "<circle" not in icone and "%23B24A18" not in icone
    assert "href='http" not in icone


def test_sufixo_da_marca_nos_dois_idiomas():
    assert 'marcaSuf: "demonstração"' in html
    assert 'marcaSuf: "demo"' in html


def test_a_demo_nao_se_chama_mais_de_operacoes():
    for pagina in (html, og):
        assert "demo de operações" not in pagina
        assert "operations demo" not in pagina


def test_links_do_site_levam_a_rota_de_agentes():
    assert "/operacoes" not in html
    assert html.count('href="https://gomio.com.br/agentes"') == 2


def test_rotulo_do_link_nos_dois_idiomas():
    assert "linkOperacoes" not in html
    assert 'data-t="linkAgentes"' in html
    assert 'linkAgentes: "Agentes de IA"' in html
    assert 'linkAgentes: "AI agents"' in html


# -- métrica de desenvolvimento fora da tela -------------------------------------


def test_custo_saiu_da_tela():
    for sobra in (
        'id="gasto"',
        "#gasto",
        "mostrarGasto",
        "gastoUsd",
        "gastoRotulo",
        "gastoTitulo",
        "data-t-title",
        "usd(",
        "usd_conversa",
        "US$",
        "pricing.yaml",
    ):
        assert sobra not in html, sobra


def test_modelo_e_latencia_sairam_do_registro():
    for sobra in (
        "d.modelo",
        "latencia_ms",
        "L.llm",
        "cacheLido",
        "semCache",
        "tarifas",
        "semcache",
        "rates_version",
    ):
        assert sobra not in html, sobra


def test_evento_de_uso_nao_desenha_nada():
    caso = html[html.index('case "usage"') :]
    caso = caso[: caso.index("break;")]
    assert "evento(" not in caso


def test_contador_de_recusas_continua():
    assert '<b id="recusas"' in html and "contarRecusa()" in html


def test_limites_nao_citam_moeda():
    assert "em dólares" not in html and "in dollars" not in html
    assert "teto diário de gasto, reservado antes de cada chamada paga" in html
    assert "a daily spend ceiling, reserved before each paid call" in html


# -- imagem de compartilhamento ---------------------------------------------------


def test_imagem_de_compartilhamento_usa_o_logo_novo():
    assert SIMBOLO_LOGO in og and WORDMARK_LOGO in og
    assert '<span class="suf">demonstração</span>' in og


def test_captura_aceita_outro_endereco_e_nao_le_o_custo():
    assert (
        'os.environ.get("AURORA_URL", "https://aurora.pgdev.com.br/?lang=pt")'
        in captura
    )
    assert "#gasto" not in captura
