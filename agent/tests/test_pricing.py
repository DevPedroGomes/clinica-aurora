"""A régua de custo, e a procedência dela.

O que estes testes protegem não é a aritmética — é o fato de que um número de
custo sem a tarifa que o produziu não serve para auditar nada. Se `rates_version`
sumir da linha, o painel continua mostrando dinheiro e ninguém consegue mais
dizer sob qual tabela aquele dinheiro foi contado.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.pricing import MICROS_POR_USD, Tabela

ANTES_DA_VIRADA = date(2026, 8, 24)
DEPOIS_DA_VIRADA = date(2026, 9, 1)


def uso(entrada=0, saida=0, leu=0, escreveu=0):
    return SimpleNamespace(
        input_tokens=entrada,
        output_tokens=saida,
        cache_read_input_tokens=leu,
        cache_creation_input_tokens=escreveu,
    )


@pytest.fixture
def tabela():
    return Tabela(
        {
            "version": "teste.1",
            "modelos": {
                "m": {
                    "input": 3.00,
                    "output": 15.00,
                    "promocao": {"ate": "2026-08-31", "input": 2.00, "output": 10.00},
                },
                "sem-promo": {"input": 1.00, "output": 5.00},
            },
            "cache": {"leitura": 0.1, "escrita": 1.25},
        }
    )


def test_input_e_output_usam_tarifas_diferentes(tabela):
    c = tabela.custo("sem-promo", uso(entrada=1_000_000), hoje=DEPOIS_DA_VIRADA)
    assert c.micros == 1 * MICROS_POR_USD
    c = tabela.custo("sem-promo", uso(saida=1_000_000), hoje=DEPOIS_DA_VIRADA)
    assert c.micros == 5 * MICROS_POR_USD


def test_cache_lido_custa_um_decimo_e_escrito_custa_um_quarto_a_mais(tabela):
    """É por isso que o cache paga: 1M lidos custam o mesmo que 100k crus."""
    lido = tabela.custo("sem-promo", uso(leu=1_000_000), hoje=DEPOIS_DA_VIRADA)
    cru = tabela.custo("sem-promo", uso(entrada=1_000_000), hoje=DEPOIS_DA_VIRADA)
    escrito = tabela.custo("sem-promo", uso(escreveu=1_000_000), hoje=DEPOIS_DA_VIRADA)

    assert lido.micros == round(cru.micros * 0.1)
    assert escrito.micros == round(cru.micros * 1.25)


def test_as_tres_contagens_de_entrada_nao_se_sobrepoem(tabela):
    """`input_tokens` já vem líquido do que foi cacheado.

    Somar as três com os multiplicadores certos dá o total. Se algum dia a API
    passar a devolver o bruto em `input_tokens`, este teste continua passando e
    o custo dobra em silêncio — por isso `cache_hit` também vai para a tela, é
    ele que denuncia a divergência antes da fatura.
    """
    junto = tabela.custo(
        "sem-promo",
        uso(entrada=100, leu=200, escreveu=300, saida=50),
        hoje=DEPOIS_DA_VIRADA,
    )
    separado = sum(
        tabela.custo("sem-promo", u, hoje=DEPOIS_DA_VIRADA).micros
        for u in (uso(entrada=100), uso(leu=200), uso(escreveu=300), uso(saida=50))
    )
    assert junto.micros == pytest.approx(separado, abs=2)


def test_a_promocao_vale_ate_a_data_e_nao_depois(tabela):
    """O dia em que o preço de introdução cai, o custo sobe 50% sem nada mudar.

    Quem lê o audit log meses depois precisa conseguir ver qual das duas tarifas
    pegou, senão a série histórica tem um degrau inexplicável no meio.
    """
    dentro = tabela.custo("m", uso(entrada=1_000_000), hoje=ANTES_DA_VIRADA)
    fora = tabela.custo("m", uso(entrada=1_000_000), hoje=DEPOIS_DA_VIRADA)

    assert dentro.promocao_aplicada is True
    assert fora.promocao_aplicada is False
    assert dentro.micros == 2 * MICROS_POR_USD
    assert fora.micros == 3 * MICROS_POR_USD
    assert tabela.custo("m", uso(), hoje=date(2026, 8, 31)).promocao_aplicada is True


def test_modelo_desconhecido_levanta_em_vez_de_custar_zero(tabela):
    """Custo que vira zero em silêncio é pior que custo nenhum.

    O painel continuaria mostrando um número, e o número estaria errado para
    baixo — exatamente o erro que ninguém vai investigar.
    """
    with pytest.raises(KeyError, match="claude-inexistente"):
        tabela.custo("claude-inexistente", uso(entrada=100))


def test_usage_incompleto_nao_derruba_o_turno(tabela):
    """Campo de cache ausente é normal quando não há cache na conversa."""
    c = tabela.custo("sem-promo", SimpleNamespace(input_tokens=10, output_tokens=5))
    assert c.micros >= 0
    assert c.cache_read_tokens == 0
    assert c.cache_hit is False


def test_toda_linha_de_custo_carrega_a_versao_da_tabela(tabela):
    c = tabela.custo("sem-promo", uso(entrada=10), hoje=DEPOIS_DA_VIRADA)
    assert c.rates_version == "teste.1"
    assert c.audit_payload()["rates_version"] == "teste.1"
    assert "usd" in c.audit_payload()


def test_a_tabela_de_verdade_cobre_o_modelo_que_a_demo_usa():
    """Guarda contra trocar `AGENT_MODEL` e esquecer do `pricing.yaml`.

    O `KeyError` acima protege em runtime, mas em runtime já é tarde: seria uma
    exceção no meio da conversa de um visitante.
    """
    from src.agent import MODEL
    from src.pricing import TABELA

    c = TABELA.custo(MODEL, uso(entrada=1000, saida=100))
    assert c.micros > 0
    assert TABELA.version
