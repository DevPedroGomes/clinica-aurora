"""Captura a Aurora em produção depois de uma conversa real.

Remarca a consulta da Ana (proposta, confirmação, gravação) e pede um
cancelamento que a regra recusa. Salva um print da página inteira para conferir
o visual em produção e um recorte do painel, em 2x, para compor o og.png.

Custa algumas chamadas pagas (cerca de US$ 0,03). Uso, num venv:
    pip install playwright
    python scripts/og/captura.py   # usa o Chrome instalado (channel="chrome")
    python scripts/og/render.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

AQUI = Path(__file__).parent
URL = "https://aurora.gomio.com.br/?lang=pt"


def esperar_resposta(pagina, antes: int) -> None:
    # A resposta terminou quando surge uma fala nova da assistente e o botão
    # de enviar volta a ficar habilitado.
    pagina.wait_for_function(
        "(n) => document.querySelectorAll('.fala.ela').length > n"
        " && !document.querySelector('#enviar').disabled",
        arg=antes,
        timeout=90_000,
    )


with sync_playwright() as p:
    navegador = p.chromium.launch(channel="chrome", headless=True)
    ctx = navegador.new_context(
        viewport={"width": 1440, "height": 1000}, device_scale_factor=2, locale="pt-BR"
    )
    pagina = ctx.new_page()
    pagina.goto(URL, wait_until="networkidle")
    pagina.wait_for_timeout(1200)
    pagina.screenshot(path=str(AQUI / "prod_inicio.png"))

    falas = pagina.locator(".fala.ela").count()
    pagina.get_by_role("button", name="Remarcar com confirmação").click()
    esperar_resposta(pagina, falas)

    falas = pagina.locator(".fala.ela").count()
    pagina.fill("#entrada", "sim")
    pagina.press("#entrada", "Enter")
    esperar_resposta(pagina, falas)
    pagina.wait_for_selector("tr.mudou", timeout=15_000)

    pagina.wait_for_timeout(1500)
    # O registro rola até o card "gravado", que é a prova que a imagem mostra.
    pagina.evaluate("""() => {
      const g = [...document.querySelectorAll('.ev.write')].pop();
      const log = document.querySelector('#log');
      log.scrollTop = g.offsetTop - log.offsetTop - 12;
    }""")
    pagina.wait_for_timeout(400)
    pagina.screenshot(path=str(AQUI / "prod_gravado.png"))
    pagina.locator(".painel-envelope .bezel").screenshot(path=str(AQUI / "painel.png"))
    print(
        "recusas:", pagina.inner_text("#recusas"), "custo:", pagina.inner_text("#gasto")
    )
    print("linha marcada:", pagina.inner_text("tr.mudou"))
    navegador.close()
