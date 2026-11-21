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
shutil.copy(src, dst)
os.environ["DATABASE_URL"] = f"sqlite:///{dst.as_posix()}"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

USERS = ["admin", "director", "deputy", "irina", "oleg", "pavel", "dmitro", "glav", "andrey", "elektro", "motor",
         "moyka", "detail", "buh"]
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


def main():
    rows, bad = [], 0
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
