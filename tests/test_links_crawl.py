"""Обход ссылок (11.11.2026): под каждой ролью открываем все пункты меню и все ссылки с этих страниц
(и на шаг глубже). Ни одной ошибки 500 и 404; пункты меню не дают «нет доступа».
Все действия в CRM — POST, поэтому GET-ссылки открывать безопасно."""
import re
from urllib.parse import urldefrag, urlparse

from .conftest import login

ROLES = [("admin", "admin123"), ("irina", "irina123"), ("oleg", "oleg123"), ("pavel", "pavel123"),
         ("dmitro", "dmitro123"), ("andrey", "andrey123"), ("director", "director123"), ("buh", "buh123")]
SKIP = ("/logout", "/login", "/static/", "/media/", "/kiosk", "/tools/files/", "/razbor/files/", "/orders/photo",
        "/api/", "/stock/labels")
HREF = re.compile(r'href="(/[^"#]*)')
MENU = re.compile(r'<nav[^>]*class="[^"]*side[^"]*"[^>]*>(.*?)</nav>', re.S)


SCRIPT = re.compile(r"<script\b.*?</script>", re.S)


def _links(html: str) -> list[str]:
    html = SCRIPT.sub("", html)   # в скриптах — селекторы вида a[href="/stock"], это не ссылки
    out = []
    for h in HREF.findall(html):
        h = urldefrag(h.replace("&amp;", "&"))[0]
        if h and not h.startswith(SKIP) and "export" not in h and "/print" not in h:
            out.append(h)
    return out


def test_crawl_all_roles(client):
    problems, visited = [], {}
    for login_, pwd in ROLES:
        client.post("/logout")
        r = login(client, login_, pwd)
        start = client.get("/")
        menu = sorted({h for h in _links(start.text) if urlparse(h).path.count("/") == 1 and h != "/"})
        for m in menu:
            r = client.get(m)
            if r.status_code != 200:
                problems.append(f"{login_}: меню {m} → {r.status_code}")
        seen, queue = set(menu), [(m, 0) for m in menu]
        while queue and len(seen) < 220:
            url, depth = queue.pop(0)
            r = client.get(url)
            if r.status_code >= 500 or r.status_code == 404:
                problems.append(f"{login_}: {url} → {r.status_code}")
                continue
            if depth >= 1 or "text/html" not in r.headers.get("content-type", ""):
                continue
            for h in _links(r.text):
                # одинаковые страницы с разными датами/фильтрами — берём по одной на путь+набор параметров
                key = urlparse(h).path + "?" + "&".join(sorted(k.split("=")[0] for k in urlparse(h).query.split("&") if k))
                if key not in seen:
                    seen.add(key)
                    queue.append((h, depth + 1))
        visited[login_] = len(seen)
    client.post("/logout")
    assert not problems, "\n".join(problems[:40])
    assert visited["admin"] > 60 and visited["andrey"] > 5, visited   # обход действительно прошёл по страницам
    print("Открыто страниц по ролям:", visited)
