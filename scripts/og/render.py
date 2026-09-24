"""Renderiza og.html em 1200x630 e grava agent/static/og.png.

Rode captura.py antes: ele gera painel.png (recorte da página em produção).
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

AQUI = Path(__file__).parent

with sync_playwright() as p:
    navegador = p.chromium.launch(channel="chrome", headless=True)
    pagina = navegador.new_page(
        viewport={"width": 1200, "height": 630}, device_scale_factor=1
    )
    pagina.goto((AQUI / "og.html").as_uri(), wait_until="networkidle")
    pagina.evaluate("document.fonts.ready")
    pagina.wait_for_timeout(600)
    pagina.screenshot(path=str(AQUI.parent.parent / "agent" / "static" / "og.png"))
    navegador.close()
