#!/usr/bin/env python3
"""
Rendert den Instagram-Post headless (Playwright/Chromium) und speichert jede
Slide als PNG (2160x2700, 2x) nach instagram/export/<season>/weekNN/.

Aufruf:  python instagram/render_post.py --week 1 [--season 2026] [--scale 2]
Voraussetzung: pip install playwright && playwright install --with-deps chromium
"""
import argparse, http.server, json, os, re, socketserver, sys, threading, urllib.parse
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POST = "instagram/post/TTT Weekly Post.dc.html"


def serve(root, port):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=root, **k)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--scale", type=int, default=2)
    a = ap.parse_args()

    season, week = a.season, a.week
    if not (season and week):
        latest_dir = os.path.join(ROOT, "instagram", "data")
        seasons = sorted(d for d in os.listdir(latest_dir) if d.isdigit())
        season = season or int(seasons[-1])
        latest = json.load(open(os.path.join(latest_dir, str(season), "latest.json")))
        week = week or int(latest["week"])
    data_rel = f"instagram/data/{season}/week{week:02d}.json"
    if not os.path.exists(os.path.join(ROOT, data_rel)):
        print("fehlt:", data_rel, file=sys.stderr); sys.exit(1)

    out_dir = os.path.join(ROOT, "instagram", "export", str(season), f"week{week:02d}")
    os.makedirs(out_dir, exist_ok=True)

    port = 8765
    httpd = serve(ROOT, port)
    url = f"http://127.0.0.1:{port}/{urllib.parse.quote(POST)}?week={week}&data=/{data_rel}"
    print("render:", url, file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1600}, device_scale_factor=a.scale)
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector('[data-render-ready="1"]', timeout=60000)
        page.wait_for_function("document.fonts && document.fonts.status === 'loaded'", timeout=30000)
        page.wait_for_timeout(1500)   # Chart/SVG settle
        slides = page.query_selector_all("[data-screen-label]")
        if not slides:
            print("keine Slides gefunden", file=sys.stderr); sys.exit(1)
        written = []
        for el in slides:
            label = el.get_attribute("data-screen-label") or "slide"
            name = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") + ".png"
            path = os.path.join(out_dir, name)
            el.screenshot(path=path, type="png")
            written.append(name)
        caption = json.load(open(os.path.join(ROOT, data_rel), encoding="utf-8")).get("autoCaption", "")
        with open(os.path.join(out_dir, "caption.txt"), "w", encoding="utf-8") as f:
            f.write(caption)
        browser.close()
    httpd.shutdown()
    print("geschrieben:", out_dir, written, file=sys.stderr)


if __name__ == "__main__":
    main()
