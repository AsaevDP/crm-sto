"""23.11.2026 (брат): карта проверки CRM — под кем зайти, что у него горит сегодня (ссылка прямо на дело),
все его разделы и наряды по этапам. Собирается из рабочей базы (копии), ссылки — на http://localhost:8810.

    .venv\\Scripts\\python scripts\\check_map.py   → docs/karta-proverki.html
"""
import html
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src, dst = ROOT / "data" / "crm.db", ROOT / "data" / "try" / "map.db"
dst.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
    a.backup(b)
os.environ["DATABASE_URL"] = f"sqlite:///{dst.as_posix()}"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app import models as M  # noqa: E402
from app.models.orders import WO_STATUSES  # noqa: E402
from app.permissions import effective_permissions, menu_for  # noqa: E402

BASE = ""   # ссылки внутри CRM: карта открывается по адресу /karta
DOT = {"red": "🔴", "orange": "🟠", "purple": "🟣", "green": "🟢"}
ORDER = ["admin", "director", "deputy", "irina", "marina", "oleg", "yuriy", "pavel", "glav", "dmitro", "andrey", "taras",
         "elektro", "motor", "moyka", "detail", "buh"]
e = html.escape


def a(href, text):
    return f'<a href="{BASE}{e(href)}" target="_blank">{e(text)}</a>'


def main():
    out = []
    with SessionLocal() as db:
        users = {u.login: u for u in db.scalars(select(M.User).where(M.User.active.is_(True)))}
        menus = {lg: menu_for(effective_permissions(db, u)) for lg, u in users.items()}
        orders = db.scalars(select(M.WorkOrder).order_by(M.WorkOrder.number)).all()
        veh = {v.id: v for v in db.scalars(select(M.Vehicle))}
        people = {u.id: u for u in db.scalars(select(M.User))}
        n_cars = len({o.vehicle_id for o in orders})
    with TestClient(app) as c:
        for lg in [x for x in ORDER if x in users] + sorted(set(users) - set(ORDER)):
            u = users[lg]
            c.post("/logout")
            r = c.post("/login", data={"login": lg, "password": lg + "123"}, follow_redirects=False)
            if r.status_code != 303:
                continue
            badges = c.get("/api/notify").json().get("badges", {})
            role = u.role.name if u.role else ""
            out.append(f'<section class="card"><h2>{e(u.full_name)} <span class="role">{e(role)}</span></h2>'
                       f'<form class="login" method="post" action="/login"><input type="hidden" name="login" value="{e(lg)}">'
                       f'<input type="hidden" name="password" value="{e(lg)}123">'
                       f'<button>Войти под ним</button> <span class="muted">логин <b>{e(lg)}</b> / пароль <b>{e(lg)}123</b></span></form>')
            items = []
            for menu, lst in badges.items():
                for x in lst:
                    gos = x.get("go") or [{"t": x["t"], "href": x["href"], "n": x["n"]}]
                    for g in gos:
                        items.append(f'<li>{DOT.get(x["c"], "")} <b>{g["n"]}</b> — {a(g["href"], g["t"])}'
                                     f'{" <span class=muted>(для сведения)</span>" if x.get("info") else ""}</li>')
            out.append("<h3>Горит сегодня — нажать и проверить</h3>" +
                       (f"<ul>{''.join(items)}</ul>" if items else '<p class="muted">Кружков нет — дел на сегодня нет.</p>'))
            links = " · ".join(a(m["href"], m["title"]) for m in menus.get(lg, []))
            out.append(f"<h3>Все разделы</h3><p>{links}</p></section>")
    # наряды по этапам — какую машину открыть, чтобы увидеть этап
    by = {}
    for o in orders:
        by.setdefault(o.status, []).append(o)
    rows = []
    for st, lst in by.items():
        cells = ", ".join(a(f"/orders/{o.id}", f"{o.number[-6:]} {veh[o.vehicle_id].make if o.vehicle_id in veh else ''}")
                          for o in lst[:12])
        rows.append(f"<tr><td><b>{e(WO_STATUSES.get(st, st) if isinstance(WO_STATUSES, dict) else st)}</b></td>"
                    f"<td>{len(lst)}</td><td>{cells}</td></tr>")
    page = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Карта проверки CRM</title><style>
body{{font-family:-apple-system,"Segoe UI",sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}}
main{{max-width:1100px;margin:0 auto;padding:28px 18px 60px}} h1{{font-size:26px;margin:0 0 4px}}
.sub{{color:#6e6e73;margin:0 0 18px}} .card{{background:#fff;border-radius:16px;padding:16px 20px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
h2{{font-size:19px;margin:0}} .role{{font-size:13px;color:#6e6e73;font-weight:500}} h3{{font-size:14px;margin:12px 0 6px;color:#3a3a3c}}
.login{{margin:6px 0 0;font-size:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
.login button{{border:0;border-radius:10px;padding:7px 14px;background:#0071e3;color:#fff;font:inherit;font-weight:600;cursor:pointer}} ul{{margin:0;padding-left:20px}} li{{margin:3px 0;font-size:14px}}
p{{font-size:14px;line-height:1.6}} a{{color:#0071e3;text-decoration:none}} a:hover{{text-decoration:underline}}
.muted{{color:#8e8e93}} table{{border-collapse:collapse;width:100%;font-size:14px;background:#fff;border-radius:12px;overflow:hidden}}
td{{padding:7px 10px;border-bottom:1px solid #ececf0;vertical-align:top}}</style></head><body><main>
<h1>Карта проверки CRM</h1>
<p class="sub">Собрано {datetime.now():%d.%m.%Y %H:%M} из рабочей базы · машин {n_cars}, нарядов {len(orders)} ·
Открыта прямо в CRM: адрес <b>/karta</b>.</p>
<div class="card"><b>Как проверять:</b> 1) у нужного сотрудника нажмите «Войти под ним» — откроется CRM под ним;
2) вернитесь на карту (адрес <b>/karta</b> или кнопка «Назад» браузера) и нажимайте строки «Горит сегодня» — каждая открывается
в новой вкладке прямо на месте дела, оно мигает; 3) сделайте действие — цифра в меню гаснет; 4) пройдите «Все разделы».
Наряды на всех этапах — таблица внизу.</div>
{''.join(out)}
<h2 style="margin-top:28px">Наряды по этапам — какую машину открыть</h2>
<table>{''.join(rows)}</table>
</main></body></html>"""
    (ROOT / "docs" / "karta-proverki.html").write_text(page, encoding="utf-8")
    print("Готово: docs/karta-proverki.html")


if __name__ == "__main__":
    main()
