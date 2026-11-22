"""22.11.2026 (брат): проверка всех цветных кружков у всех сотрудников.

Под каждым сотрудником берёт его кружки (/api/notify), открывает ссылку каждого и проверяет, что страница открылась
и на ней есть то, что обводится рамкой: блок наряда, строка, плитка-фильтр, якорь (#…) или карточка того же цвета.
Работает на КОПИИ базы (по умолчанию data/try/crm.db → data/try/check.db), рабочую не трогает.

    .venv\\Scripts\\python scripts\\check_badges.py [путь к базе] > отчёт.md
"""
import os
import re
import shutil
from html import unescape as html_unescape
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent
src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "try" / "crm.db"
dst = ROOT / "data" / "try" / "check.db"
import sqlite3   # безопасная копия, даже если базу сейчас пишет сервер
with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
    a.backup(b)
os.environ["DATABASE_URL"] = f"sqlite:///{dst.as_posix()}"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

USERS = ["admin", "director", "deputy", "irina", "oleg", "pavel", "dmitro", "glav", "andrey", "elektro", "motor",
         "moyka", "detail", "buh", "marina", "taras", "yuriy"]
COLOR = {"red": "🔴", "orange": "🟠", "purple": "🟣", "green": "🟢"}


def has_block(html: str, sel: str) -> bool:
    if sel.startswith("#"):
        return f'id="{sel[1:]}"' in html
    m = re.search(r'\[data-id="([^"]+)"\]', sel)
    if m:
        return f'data-id="{m.group(1)}"' in html
    cls = sel.lstrip(".").split("[")[0]
    return f'class="{cls}' in html or f" {cls}" in html


def check(c, x) -> tuple[str, str]:
    """→ (что обведено, ошибка)"""
    link = x["href"]
    u = urlsplit(link)
    r = c.get(link)
    if r.status_code != 200:
        return "", f"страница {r.status_code}"
    html = r.text
    found = []
    if x.get("wo"):
        wid = x["wo"][0]
        page = html if u.path == f"/orders/{wid}" else c.get(f"/orders/{wid}").text
        if x.get("blk"):
            if not has_block(page, x["blk"]):
                return "", f"в наряде нет блока {x['blk']}"
            found.append(f"блок {x['blk']}")
        rows = [s for s in x.get("rows", []) if has_block(page, s)]
        if rows:
            found.append(f"строк: {len(rows)}")
        if len(x["wo"]) > 1:
            n = sum(1 for w in x["wo"] if f'href="/orders/{w}' in html)
            found.append(f"нарядов в списке: {n} из {len(x['wo'])}")
            if not n:
                return ", ".join(found), "в списке нет ни одного наряда"
    if u.fragment:
        if f'id="{u.fragment}"' not in html:
            return ", ".join(found), f"на странице нет #{u.fragment}"
        found.append(f"#{u.fragment}")
    if u.query and not x.get("wo") or (u.query and len(x.get("wo", [])) > 1):
        q = dict(parse_qsl(u.query))
        tiles = re.findall(r'<a[^>]*class="[^"]*(?:ptile|tile)[^"]*"[^>]*href="([^"]+)"|<a[^>]*href="([^"]+)"[^>]*class="[^"]*(?:ptile|tile)', html)
        ok = any(all(dict(parse_qsl(urlsplit(html_unescape(a or b)).query)).get(k) == v for k, v in q.items()) for a, b in tiles)
        if ok:
            found.append("плитка")
        elif re.search(r'class="(?:ptile|tile)[^"]*\bon\b', html):
            found.append("плитка (открыта)")
    for p, s in x.get("sel", []):
        if p in (u.path, f"{u.path}?{u.query}"):
            m = re.search(r'\[data-(\w+)="([^"]*)"\]', s)
            hit = (f'data-{m.group(1)}="{m.group(2)}"' in html) if m else any(
                cl.lstrip(".").split(":")[0].split(" ")[0] in html for cl in s.replace("tr.", ".").split(","))
            if hit:
                found.append("строки")
                break
    if f"rim-{x['c']}" in html:
        found.append("карточки цвета")
    if not found:
        return "", "на странице нечего обвести"
    return ", ".join(found), ""


TILE_RE = re.compile(r'<a\b([^>]*\bclass="[^"]*\bptile\b[^"]*"[^>]*)>(.*?)</a>', re.S)


def tiles_of(html: str) -> list[dict]:
    out = []
    for attrs, body in TILE_RE.findall(html):
        m = re.search(r'href="([^"]*)"', attrs)
        n = re.search(r"<b[^>]*>\s*([\d\s ]+)\s*</b>", body)
        t = re.sub(r"<[^>]+>", " ", body)
        out.append({"href": html_unescape(m.group(1)) if m else "", "n": int(re.sub(r"\D", "", n.group(1))) if n else None,
                    "t": " ".join(t.split())[:40]})
    return out


def tile_check(c, x) -> str:
    """23.11.2026 (брат): горит кружок — на его странице обязательно есть своя плитка (она мигает), число совпадает.
    → '' если всё хорошо, иначе текст ошибки; для страниц без плиток (наряд, «Запись») — ''."""
    u = urlsplit(x["href"])
    if x.get("tile"):   # 23.11.2026: своя плитка по названию — на странице кружка (или в его разделе, если ведёт в наряд)
        page = x["menu"] if (u.path.startswith("/orders/") and u.path.count("/") == 2) else (f"{u.path}?{u.query}" if u.query else u.path)
        html = c.get(page).text
        spans = re.findall(r'class="ptile[^"]*"[^>]*>\s*<span class="ptile-t">([^<]+)</span>\s*<b[^>]*>([^<]*)</b>', html)
        found = [(t.strip(), n) for t, n in spans if t.strip() in x["tile"]]
        if not found:
            return f"на {page} нет плитки {x['tile']} (есть: {', '.join(t for t, _ in spans[:8])})"
        nums = [int(re.sub(r"\D", "", n) or 0) for _, n in found]
        if sum(nums) == 0:
            return f"плитка {found} = 0, а кружок = {x['n']}"
        return f"ok · плитка {'+'.join(t for t, _ in found)} = {'+'.join(map(str, nums))}, кружок = {x['n']}"
    if u.path.startswith("/orders/") and u.path.count("/") == 2:
        return ""   # ведёт прямо в наряд — там плиток нет, обводится блок / строка
    html = c.get(f"{u.path}?{u.query}" if u.query else u.path).text
    ts = tiles_of(html)
    if not ts:
        return ""
    q = dict(parse_qsl(u.query))
    match = [t for t in ts if urlsplit(t["href"]).path in ("", u.path)
             and all(dict(parse_qsl(urlsplit(t["href"]).query)).get(k) == v for k, v in q.items())] if q else []
    if not match:
        return f"нет плитки для «{x['href']}» (плитки: {', '.join(t['t'] for t in ts[:6])})"
    t = match[0]
    if t["n"] is not None and t["n"] != x["n"] and not x.get("also"):
        return f"плитка «{t['t']}» = {t['n']}, кружок = {x['n']}"
    return ""


def main():
    rows, bad = [], 0
    strict = "--tiles" in sys.argv
    with TestClient(app) as c:
        for login in USERS:
            c.post("/logout")
            r = c.post("/login", data={"login": login, "password": login + "123"}, follow_redirects=False)
            if r.status_code != 303:
                rows.append(f"| {login} | — | не вошёл | | |")
                continue
            badges = c.get("/api/notify").json()["badges"]
            if not badges:
                rows.append(f"| {login} | — | кружков нет | | |")
            for menu, lst in badges.items():
                for x in lst:
                    if strict:
                        x["menu"] = menu
                        err = tile_check(c, x)
                        if err.startswith("ok"):
                            rows.append(f"| {login} | {menu} {COLOR[x['c']]}{x['n']} | {x['t']} | `{x['href']}` | ✓ {err[5:]} |")
                            continue
                        bad += bool(err)
                        rows.append(f"| {login} | {menu} {COLOR[x['c']]}{x['n']} | {x['t']} | `{x['href']}` | "
                                    f"{'❌ ' + err if err else '✓'} |")
                        continue
                    if x.get("info"):
                        rows.append(f"| {login} | {menu} {COLOR[x['c']]}{x['n']} | {x['t']} | `{x['href']}` | для сведения — без рамки |")
                        continue
                    what, err = check(c, x)
                    bad += bool(err)
                    rows.append(f"| {login} | {menu} {COLOR[x['c']]}{x['n']} | {x['t']} | `{x['href']}` | "
                                f"{'❌ ' + err if err else '✓ ' + what} |")
    print("| Вход | Кружок | Что значит | Куда ведёт | Что обведено |")
    print("|---|---|---|---|---|")
    print("\n".join(rows))
    print(f"\nОшибок: {bad}")


if __name__ == "__main__":
    main()
