// Общие мелочи интерфейса. Без внешних библиотек.
(function () {
  async function post(url, data) {
    const r = await fetch(url, {
      method: 'POST', body: new URLSearchParams(data),
      headers: { 'x-requested-with': 'fetch' }, credentials: 'same-origin',
    });
    if (r.status === 401) { location.href = '/login'; return null; }
    return r.ok ? r.json() : null;
  }

  // 11.11.2026: после действия на той же странице (сохранил, подтвердил, нажал «Да») страница НЕ прыгает наверх.
  // Перед отправкой формы запоминаем, где стояли; если сервер вернул на ту же страницу — встаём туда же.
  // Перешли на другую страницу (следующий этап, другой раздел) — ничего не трогаем.
  const SCROLL_KEY = 'crm-keep-scroll';
  const scrollers = () => [...document.querySelectorAll('.ov-col, .sticky-scroll')];
  // 22.11.2026 (брат): недописанный текст в ДРУГИХ полях страницы не пропадает после отправки формы. Пример: пишешь
  // проблему, отправил сообщение — страница перезагрузилась, текст проблемы был стёрт. Перед уходом запоминаем
  // изменённые, но не отправленные поля (кроме самой отправленной формы), после перезагрузки той же страницы — возвращаем.
  const DRAFT_KEY = 'crm-drafts';
  const draftFields = () => [...document.querySelectorAll('main textarea, main input')].filter(el =>
    el.tagName === 'TEXTAREA' || ['text', 'number', 'tel', 'email', 'url', ''].includes((el.getAttribute('type') || '').toLowerCase()));
  // Ключ поля — по форме (id или адрес) и имени, а не по номеру формы: после отправки на странице может стать
  // больше форм (новое сообщение, новая деталь)
  const formKey = f => f ? (f.id || f.getAttribute('action') || 'form') : '-';
  const fieldKey = el => {
    const base = `${formKey(el.form)}|${el.id}|${el.name}`;
    const same = draftFields().filter(x => `${formKey(x.form)}|${x.id}|${x.name}` === base);
    return `${base}|${same.indexOf(el)}`;
  };
  let lastSent = null, lastSentAt = 0;
  document.addEventListener('submit', e => { lastSent = e.target; lastSentAt = Date.now(); }, true);
  function saveDrafts(sent) {
    const items = draftFields().filter(el => !el.readOnly && !el.disabled && !el.classList.contains('search')
      && el.value.trim() && el.value !== el.defaultValue && !(sent && el.form === sent))
      .map(el => ({ k: fieldKey(el), v: el.value }));
    try {
      if (items.length) sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ p: location.pathname, t: Date.now(), items }));
      else sessionStorage.removeItem(DRAFT_KEY);
    } catch (err) {}
  }
  function remember(f) {
    if ((f.method || '').toLowerCase() !== 'post' || (f.target && f.target !== '_self')) return;
    saveDrafts(f instanceof HTMLFormElement ? f : (Date.now() - lastSentAt < 60000 ? lastSent : null));
    try {
      sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ p: location.pathname, y: window.scrollY, t: Date.now(),
        cols: scrollers().map(c => c.scrollTop) }));
    } catch (err) {}
  }
  (function restoreDrafts() {
    let s = null;
    try { s = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || 'null'); sessionStorage.removeItem(DRAFT_KEY); } catch (e) {}
    if (!s || s.p !== location.pathname || Date.now() - s.t > 60000) return;
    const byKey = new Map(draftFields().map(el => [fieldKey(el), el]));
    s.items.forEach(({ k, v }) => {
      const el = byKey.get(k);
      if (!el || el.value !== el.defaultValue) return;
      el.value = v;
      let d = el.closest('details'); while (d) { d.open = true; d = d.parentElement && d.parentElement.closest('details'); }
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
  })();
  document.addEventListener('submit', e => {
    if (e.target instanceof HTMLFormElement && !e.defaultPrevented) remember(e.target);
  });
  // 13.11.2026: действия, которые сохраняются в фоне и потом перезагружают страницу (проблема, фото, «Да / Нет»),
  // тоже встают на прежнее место: вызывать crmReload() вместо location.reload()
  window.crmReload = function () { remember({ method: 'post' }); location.reload(); };
  // form.submit() из скриптов (выбор в списке и т.п.) события submit не даёт — запоминаем и там
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () { remember(this); return nativeSubmit.call(this); };
  (function restore() {
    let s = null;
    try { s = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || 'null'); sessionStorage.removeItem(SCROLL_KEY); } catch (e) {}
    if (!s || s.p !== location.pathname || Date.now() - s.t > 20000) return;
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
    // Сообщение «Сохранено» — поверх страницы, чтобы оно не сдвигало её вниз; само исчезает
    const fl = document.querySelector('main .flash[role=status]');
    if (fl) {
      fl.classList.add('float'); document.body.appendChild(fl);
      setTimeout(() => fl.classList.add('gone'), fl.classList.contains('error') ? 6000 : 3000);
      setTimeout(() => fl.remove(), 7000);
      fl.addEventListener('click', () => fl.remove());
    }
    const go = () => {
      window.scrollTo(0, s.y);
      scrollers().forEach((c, i) => { if (s.cols && s.cols[i]) c.scrollTop = s.cols[i]; });
    };
    go();
    requestAnimationFrame(go);
    window.addEventListener('load', () => { go(); setTimeout(go, 60); });
  })();

  // Формы «один раз» (оплата, возврат, выдача): после нажатия кнопка гаснет — второй клик не отправит форму снова.
  // На сервере всё равно стоит защита меткой формы — это для спокойствия на медленном Wi-Fi.
  document.addEventListener('submit', e => {
    const f = e.target;
    if (!(f instanceof HTMLFormElement) || !f.classList.contains('one-shot')) return;
    if (f.dataset.sent) { e.preventDefault(); return; }
    f.dataset.sent = '1';
    setTimeout(() => f.querySelectorAll('button:not([type=button])').forEach(b => { b.disabled = true; }), 0);
  }, true);

  // Переключатели прав роли — сохраняются сразу
  document.querySelectorAll('input[data-perm][data-url]').forEach(sw => {
    sw.addEventListener('change', async () => {
      const res = await post(sw.dataset.url, { perm: sw.dataset.perm, on: sw.checked ? '1' : '' });
      if (!res || !res.ok) { sw.checked = !sw.checked; alert('Не сохранилось'); }
    });
  });

  // 3-позиционные права сотрудника: как у роли / да / нет
  document.querySelectorAll('[data-user-perm]').forEach(seg => {
    seg.addEventListener('click', async e => {
      const b = e.target.closest('button[data-mode]'); if (!b) return;
      const res = await post(seg.dataset.url, { perm: seg.dataset.userPerm, mode: b.dataset.mode });
      if (!res || !res.ok) { alert('Не сохранилось'); return; }
      seg.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', x === b));
      const st = document.querySelector(`[data-state="${seg.dataset.userPerm}"]`);
      if (st) { st.textContent = res.effective ? 'есть доступ' : 'нет доступа'; st.classList.toggle('on', res.effective); }
    });
  });

  // Свой цвет: палитра выбирает значение радио-кнопки «свой»
  document.querySelectorAll('[data-custom-picker]').forEach(p => {
    const radio = p.parentElement.querySelector('[data-custom]');
    p.addEventListener('input', () => { radio.value = p.value; radio.checked = true; preview(p.value); });
  });
  // Примерка цвета роли прямо на странице
  function preview(color) {
    const css = /^#/.test(color) ? color : `var(--${color})`;
    document.documentElement.style.setProperty('--accent', css);
  }
  document.querySelectorAll('.colors input[type=radio]').forEach(r => r.addEventListener('change', () => preview(r.value)));

  // Сообщение «Сохранено» исчезает само
  const fl = document.querySelector('.flash:not(.error)');
  if (fl) setTimeout(() => { fl.style.transition = 'opacity .4s'; fl.style.opacity = '0'; setTimeout(() => fl.remove(), 400); }, 2500);

  // Кнопка «Копировать» (VIN и т.п., 12.11.2026). По Wi-Fi (http) буфер обмена браузер не даёт — запасной способ
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-copy]'); if (!b) return;
    const txt = b.dataset.copy, was = b.textContent;
    const ok = () => { b.textContent = 'Скопировано ✓'; setTimeout(() => { b.textContent = was; }, 1500); };
    const old = () => {
      const t = document.createElement('textarea'); t.value = txt; t.style.position = 'fixed'; t.style.opacity = '0';
      document.body.appendChild(t); t.select(); try { document.execCommand('copy'); ok(); } catch (err) {} t.remove();
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(txt).then(ok, old); else old();
  });

  // ===== Уведомления в углу экрана: исчезают ТОЛЬКО по крестику =====
  // Открытые окошки хранятся в браузере и показываются снова на каждой странице, пока их не закроют.
  // 12.11.2026: закрытые крестиком (или «Закрыть все») больше не всплывают — их номера помнит браузер;
  // новое уведомление — новый номер, оно показывается. Список стирает страница входа (другой сотрудник).
  const KEY = 'crm-toasts', XKEY = 'crm-toasts-closed';
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; } }
  function save(list) { try { localStorage.setItem(KEY, JSON.stringify(list.slice(-20))); } catch (e) {} }
  function closedIds() { try { return JSON.parse(localStorage.getItem(XKEY) || '[]'); } catch (e) { return []; } }
  function markClosed(ids) {
    const s = closedIds(); ids.forEach(i => { if (!s.includes(i)) s.push(i); });
    try { localStorage.setItem(XKEY, JSON.stringify(s.slice(-400))); } catch (e) {}
  }
  function read(t) { if (t.readUrl) fetch(t.readUrl, { method: 'POST', headers: { 'x-requested-with': 'fetch' } }).catch(() => {}); }
  // 22.11.2026 (брат): «Свернуть» — окошко прячется в кнопку справа внизу «🔔 N · Развернуть», его можно снова
  // открыть и перечитать. Свёрнутые помнит браузер (crm-toasts-min). Новое уведомление показывается как обычно;
  // свернули его — оно встаёт первым в общей свёртке.
  const MKEY = 'crm-toasts-min';
  function minIds() { try { return JSON.parse(localStorage.getItem(MKEY) || '[]'); } catch (e) { return []; } }
  function setMin(ids) { try { localStorage.setItem(MKEY, JSON.stringify(ids.slice(-100))); } catch (e) {} }
  function minimize(ids) {
    const m = minIds().filter(i => !ids.includes(i));
    setMin([...m, ...ids]);   // последние свёрнутые — в конце списка = первые при разворачивании
    ids.forEach(i => { const el = document.querySelector(`.toast[data-id="${CSS.escape(i)}"]`); if (el) el.hidden = true; });
    sync();
  }
  function expand() {
    const m = minIds(), s = stack(), head = s.querySelector('.toasts-head');
    setMin([]);
    // свёрнутые — сверху вниз от последнего свёрнутого к первому, под ними — те, что и так были открыты
    m.forEach(i => { const el = s.querySelector(`.toast[data-id="${CSS.escape(i)}"]`); if (el) { el.hidden = false; head.after(el); } });
    sync();
  }
  function pill() {
    let p = document.querySelector('.toasts-pill');
    if (!p) {
      p = document.createElement('button'); p.type = 'button'; p.className = 'toasts-pill';
      p.title = 'Развернуть уведомления'; p.addEventListener('click', expand);
      document.body.appendChild(p);
    }
    return p;
  }
  function sync() {   // «Закрыть все» / «Свернуть все» — когда открытых окошек два и больше; пустая стопка не видна
    const s = document.querySelector('.toasts'); if (!s) return;
    const all = [...s.querySelectorAll('.toast')];
    const open = all.filter(el => !el.hidden).length, hid = all.length - open;
    s.hidden = !open;
    s.querySelector('.toasts-head').hidden = open < 2;
    const p = pill();
    p.hidden = !hid;
    document.body.classList.toggle('has-toast-pill', !!hid);
    p.innerHTML = `<span aria-hidden="true">🔔</span> <b>${hid}</b> · Развернуть`;
  }
  function closeAll() {
    const ids = [...document.querySelectorAll('.toasts .toast:not([hidden])')].map(el => el.dataset.id);
    const list = load(), gone = list.filter(t => ids.includes(t.id));
    markClosed(ids); gone.forEach(read); save(list.filter(t => !ids.includes(t.id)));
    ids.forEach(i => { const el = document.querySelector(`.toast[data-id="${CSS.escape(i)}"]`); if (el) el.remove(); });
    sync();
  }
  function stack() {
    let s = document.querySelector('.toasts');
    if (!s) {
      s = document.createElement('div'); s.className = 'toasts'; s.setAttribute('aria-live', 'polite');
      const head = document.createElement('div'); head.className = 'toasts-head';
      const mn = document.createElement('button');
      mn.type = 'button'; mn.className = 'toasts-all'; mn.innerHTML = '<span aria-hidden="true">⌄</span> Свернуть все';
      mn.addEventListener('click', () => minimize([...s.querySelectorAll('.toast:not([hidden])')].map(el => el.dataset.id)));
      const all = document.createElement('button');
      all.type = 'button'; all.className = 'toasts-all'; all.innerHTML = '<span aria-hidden="true">✕</span> Закрыть все';
      all.addEventListener('click', closeAll);
      head.append(mn, all); s.appendChild(head); document.body.appendChild(s);
    }
    return s;
  }
  function render(t) {
    if (document.querySelector(`.toast[data-id="${CSS.escape(t.id)}"]`)) return;
    const el = document.createElement('div');
    el.className = 'toast'; el.dataset.id = t.id; el.setAttribute('role', 'status');
    const body = document.createElement(t.href ? 'a' : 'div');
    body.className = 'toast-body'; if (t.href) body.href = t.href;
    [['footnote', t.kicker], ['b', t.title], ['subhead', t.text]].forEach(([cls, txt]) => {
      if (!txt) return;
      const x = document.createElement(cls === 'b' ? 'b' : 'span'); if (cls !== 'b') x.className = cls;
      x.textContent = txt; body.appendChild(x);
    });
    if (t.tone) el.dataset.tone = t.tone;
    const close = document.createElement('button');
    close.className = 'toast-x'; close.type = 'button'; close.title = 'Закрыть'; close.setAttribute('aria-label', 'Закрыть'); close.textContent = '×';
    const done = () => {
      markClosed([t.id]); save(load().filter(x => x.id !== t.id)); el.remove(); sync(); read(t);
    };
    close.addEventListener('click', done);
    const mn = document.createElement('button');
    mn.className = 'toast-x toast-min'; mn.type = 'button'; mn.title = 'Свернуть'; mn.setAttribute('aria-label', 'Свернуть'); mn.textContent = '⌄';
    mn.addEventListener('click', () => minimize([t.id]));
    const ctl = document.createElement('div'); ctl.className = 'toast-ctl'; ctl.append(close, mn);
    el.append(body, ctl);
    if (minIds().includes(t.id)) el.hidden = true;
    // Кнопка действия прямо в окошке (11.11.2026): например, слесарю — «Запросить выдачу» одним кликом
    if (t.action && t.action.url) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'btn btn-sm toast-act'; b.textContent = t.action.label;
      b.addEventListener('click', async () => {
        b.disabled = true; b.textContent = 'Отправляю…';
        try { await fetch(t.action.url, { method: 'POST', headers: { 'x-requested-with': 'fetch' } }); b.textContent = t.action.done || 'Готово'; }
        catch (e) { b.textContent = 'Не получилось'; }
        setTimeout(done, 1200);
      });
      el.appendChild(b);
    }
    const s = stack();
    s.querySelector('.toasts-head').after(el);   // новое — сверху, сразу видно
    sync();
  }
  window.crmToast = function (t) {
    if (!t || !t.id || closedIds().includes(t.id)) return;   // закрытое не всплывает снова
    const list = load();
    if (!list.some(x => x.id === t.id)) { list.push(t); save(list); }
    render(t);
  };
  load().forEach(render);
  sync();
})();

// ===== Панель дат: горячие клавиши (10.11.2026) =====
// ← → — назад / вперёд, Т — сегодня, Д Н М К Г — текущие день, неделя, месяц, квартал, год.
// Работает на русской и английской раскладке; не мешает вводу в поля.
(function () {
  const MAP = { 'arrowleft': 'prev', 'arrowright': 'next', 'т': 'today', 't': 'today',
    'д': 'day', 'd': 'day', 'н': 'week', 'w': 'week', 'м': 'month', 'm': 'month', 'к': 'quarter', 'q': 'quarter',
    'г': 'year', 'y': 'year', 'в': 'all', 'a': 'all' };
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    const el = document.activeElement;
    if (el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable)) return;
    const bar = document.querySelector('[data-period-bar]'); if (!bar) return;
    const k = MAP[(e.key || '').toLowerCase()]; if (!k) return;
    const a = bar.querySelector(`[data-pkey="${k}"]`); if (!a) return;
    e.preventDefault(); a.click();
  });
  // Календарь закрывается кликом мимо
  document.addEventListener('click', e => {
    document.querySelectorAll('details.pcal[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; });
  });
  // 26.11.2026 (брат): окно выбора периода не вылезает за край экрана (раньше — ползунок внизу страницы);
  // открытый период в списке — сразу виден
  document.querySelectorAll('details.pcal').forEach(d => d.addEventListener('toggle', () => {
    const pop = d.querySelector('.pcal-pop');
    if (!d.open || !pop) return;
    pop.style.left = ''; pop.style.right = ''; pop.style.transform = '';
    if (getComputedStyle(pop).position !== 'absolute') return;
    const r = pop.getBoundingClientRect(), pad = 12;
    if (r.right > innerWidth - pad) pop.style.left = (parseFloat(getComputedStyle(pop).left) || 0) - (r.right - innerWidth + pad) + 'px';
    const on = pop.querySelector('.pcal-ch.on, .pw-wk.on'), box = on && on.parentElement;
    if (on && box) box.scrollTop = on.offsetTop - box.offsetTop - box.clientHeight / 2 + on.offsetHeight / 2;
  }));
})();
// ===== Бренды (11.11.2026): выбор из общего списка, «＋ Новый бренд…» добавляет для всех =====
(function () {
  function ensure(sel, name) {
    if (!name) { sel.value = ''; return; }
    const low = name.toLowerCase();
    let opt = [...sel.options].find(o => o.value.toLowerCase() === low);
    if (!opt) { opt = new Option(name, name); sel.insertBefore(opt, sel.querySelector('option[value="__new"]')); }
    sel.value = opt.value;
  }
  window.crmSetBrand = ensure;
  document.addEventListener('focusin', e => { const s = e.target.closest && e.target.closest('select[data-brand]'); if (s) s.dataset.prev = s.value; });
  document.addEventListener('change', async e => {
    const sel = e.target.closest && e.target.closest('select[data-brand]');
    if (!sel || sel.value !== '__new') return;
    const name = (prompt('Новый бренд — появится в списке у всех сотрудников:') || '').trim();
    if (!name) { sel.value = sel.dataset.prev || ''; return; }
    const r = await fetch('/api/brands', { method: 'POST', body: new URLSearchParams({ name }), headers: { 'x-requested-with': 'fetch' } });
    if (!r.ok) { alert('Не сохранилось'); sel.value = sel.dataset.prev || ''; return; }
    const j = await r.json();
    document.querySelectorAll('select[data-brand]').forEach(s => { const keep = s.value; ensure(s, j.name); if (s !== sel) s.value = keep === '__new' ? '' : keep; });
    ensure(sel, j.name);
  });
})();


// 22.11.2026 (брат): «‹ Назад» на каждой странице — на предыдущую страницу, откуда пришли.
// Помним путь в sessionStorage (только своя вкладка). Та же страница после «Сохранить» (POST → та же страница) и окошки
// (?edit=, ?free=…) — не новый шаг, поэтому «Назад» сразу уводит туда, откуда пришли, а не на ту же страницу до сохранения.
// Истории нет (страницу открыли первой) — в раздел страницы (её прежняя ссылка a.back), нет и её — кнопка скрыта.
(function navBack() {
  const btn = document.getElementById('nav-back');
  if (!btn) return;
  // 22.11.2026 (брат): шаг пути — СТРАНИЦА (адрес без ?…), а не каждое нажатие на ней. Переключили вкладку, плитку,
  // день, фильтр — это та же страница: запоминаем её последний вид, но нового шага нет. «Назад» уводит на предыдущую
  // страницу (в том виде, в каком её оставили), а не отменяет нажатия по одному.
  const KEY = 'crm-nav2', SKIP = ['edit', 'mv', 'free', 'fd', 't'];
  // 25.11.2026 (брат): параметр, который открывает ДРУГОЙ объект на том же адресе, — это новая страница, а не вид
  // (табель сотрудника /timesheet?user=… — отдельный шаг: «Назад» из него ведёт в общий табель).
  const OWN = { '/timesheet': ['user'] };
  const norm = href => {
    const u = new URL(href, location.origin);
    SKIP.forEach(k => u.searchParams.delete(k));
    u.searchParams.sort();
    const id = (OWN[u.pathname] || []).map(p => u.searchParams.get(p) || '').join('|');
    return { k: u.pathname + (id.replace(/\|/g, '') ? '#' + id : ''), u: u.pathname + (u.searchParams.toString() ? '?' + u.searchParams : '') };
  };
  let st = [];
  try { st = JSON.parse(sessionStorage.getItem(KEY) || '[]').filter(x => x && x.k); } catch (e) {}
  const cur = norm(location.href);
  const top = st[st.length - 1];
  if (top && top.k === cur.k) top.u = cur.u;                     // та же страница — только обновили её вид
  else if (st.length > 1 && st[st.length - 2].k === cur.k) { st.pop(); st[st.length - 1].u = cur.u; }   // вернулись назад
  else st.push(cur);
  st = st.slice(-50);
  try { sessionStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {}
  // 25.11.2026 (брат): плитки — меню раздела («Табель», «Инструмент»): открыта плитка — «Назад» сначала к обычному
  // виду раздела (base из data-menu-base), а уже оттуда — на прежнюю страницу
  const menu = document.querySelector('.content [data-menu-base]');
  if (menu) {
    const base = norm(menu.dataset.menuBase);
    if (base.k === cur.k && base.u !== cur.u && menu.querySelector('.ptile.on')) {
      btn.href = menu.dataset.menuBase;
      btn.title = 'Вернуться к разделу';
      btn.hidden = false;
      return;
    }
  }
  const own = document.querySelector('.content a.back');   // прежняя ссылка «‹ Раздел» — запасной путь
  const prev = st.length > 1 ? st[st.length - 2].u : (own ? own.getAttribute('href') : '');
  if (!prev) return;
  btn.href = prev;
  btn.title = 'Вернуться на предыдущую страницу';
  btn.hidden = false;
  btn.addEventListener('click', e => {
    if (st.length > 1) {   // уходим на предыдущую — убираем текущую из пути
      e.preventDefault();
      st.pop();
      try { sessionStorage.setItem(KEY, JSON.stringify(st)); } catch (er) {}
      location.href = prev;
    }
  });
})();

// ===== 22.11.2026 (брат): кружок → дело. Бегущая рамка цвета кружка на том, где нужно действовать, пока дело не
// сделано (цифры приходят с /api/notify каждые 15 с — сделали дело, цифра погасла, рамка ушла). Что обводим:
//  • плитку-фильтр, на которую ведёт кружок (ссылка с тем же адресом);
//  • блок по якорю из ссылки (#asks, #now, #zp-sig…);
//  • на странице кружка — карточки того же цвета (.rim-red / .rim-orange / .rim-green);
//  • наряды кружка: в списке «Наряды» и на странице кружка — каждый наряд (карточку со ссылкой на него);
//    в самом наряде — нужный блок (blk) и строки (rows).
(function () {
  const ORDER = ['red', 'orange', 'purple', 'green'];
  const BOX = '.ocard, .card, .rd-row, .ptile, .tile, tr, li';
  const key = u => { const x = new URL(u, location.origin); return x.pathname + x.search; };
  function apply(map) {
    const want = new Map();   // элемент → цвет (самый срочный)
    const btnColor = new Map();   // 23.11.2026: кнопке — свой цвет перелива (x.btn: «Согласовал с клиентом» — синий)
    const put = (el, c) => { if (!el || el.closest('.sidebar, .tabbar, .more-menu, .toasts, #nav-back, a.back')) return;
      const was = want.get(el); if (!was || ORDER.indexOf(c) < ORDER.indexOf(was)) want.set(el, c); };
    const main = document.querySelector('main') || document.body;
    const here = location.pathname, hereFull = here + location.search;
    Object.entries(map || {}).forEach(([menu, list]) => (list || []).forEach(x => {
      // 23.11.2026 (брат): «для сведения» тоже подсвечивается — горит кружок, значит мигает и плитка
      const local = [], add = el => { if (el) local.push(el); };
      const links = [x.href, ...(x.also || [])].filter(Boolean);
      links.forEach(h => {
        const u = new URL(h, location.origin);
        // плитки и ссылки на этот же отбор (не сама текущая страница целиком)
        if (!(x.tile || []).length && u.search && u.pathname === here) main.querySelectorAll('a[href]').forEach(a => {
          const href = a.getAttribute('href'); if (!href || href.startsWith('#')) return;
          const t = new URL(a.href); if (t.pathname !== u.pathname || a.closest('.wo-steps')) return;
          const tile = a.closest('.ptile, .tile'); if (!tile) return;   // только плитки (не кнопки периода и т. п.)
          if ([...u.searchParams].every(([k, v]) => t.searchParams.get(k) === v)) add(tile);
        });
        // заголовок группы по якорю (#all, #clients…) не обводим — только окошки / карточки
        if (u.pathname === here && u.hash) { const h = document.getElementById(u.hash.slice(1)); if (h && !h.matches('.group-title, h1, h2, h3, .title-2, .title-3')) add(h); }
        if (u.pathname === here) main.querySelectorAll('.rim.rim-' + x.c).forEach(el => add(el));
      });
      const onTarget = links.some(h => new URL(h, location.origin).pathname === here);
      // 23.11.2026 (брат): своя плитка кружка (по названию) — на странице кружка и в его разделе меню мигает всегда
      if ((x.tile || []).length && (onTarget || here === menu))
        main.querySelectorAll('.ptile').forEach(t => { const nm = (t.querySelector('.ptile-t')?.textContent || '').trim();
          // 23.11.2026 (брат): плитка с нулём не мигает никогда (на «Записи» мигала «Не приехали 0»)
          const num = parseInt(((t.querySelector('b') || {}).textContent || '').replace(/\D/g, ''), 10) || 0;
          if (x.tile.includes(nm) && num > 0) add(t); });
      (x.wo || []).forEach(id => {
        if (here === '/orders/' + id) {
          // 23.11.2026 (брат): есть кнопка / строка дела — обводим её, весь блок нет (квадратная рамка блока раздражает)
          const rowEls = (x.rows || []).flatMap(s => [...main.querySelectorAll(s)]).filter(el => el.offsetParent !== null || el.tagName === 'SUMMARY');
          if (rowEls.length) rowEls.forEach(el => add(el));
          // 23.11.2026 (брат): «для сведения» весь блок не обводит — только свои строки (или ничего)
          else if (x.blk && !x.info) add(document.querySelector(x.blk));
        } else if ((here === '/orders' && menu === '/orders') || onTarget) {
          main.querySelectorAll(`a[href="/orders/${id}"], a[href^="/orders/${id}#"], a[href^="/orders/${id}?"]`)
            .forEach(a => add(a.matches('.ocard') ? a : (a.closest(BOX) || a)));
        }
      });
      (x.sel || []).forEach(([p, s]) => { if (p === here || p === hereFull) main.querySelectorAll(s).forEach(el => add(el)); });
      local.forEach(el => { put(el, x.c); if (x.btn) btnColor.set(el, 'var(--' + x.btn + ')'); });
    }));
    // 22.11.2026 (брат): обводим только сами окошки и строки — блок / раздел вокруг них (якорь #…, блок наряда) не
    // обводим, если внутри уже есть обведённое
    const els = [...want.keys()];
    els.forEach(el => { if (els.some(o => o !== el && el.contains(o))) want.delete(el); });
    const EDGE = '.part, .work, .wq-card, .lo-card, .rc-group, tr';   // + «Склад»: приход, лишнее, «Всегда в наличии»   // строки и окошки с полоской сбоку — мигает только край
    const CLS = ['need', 'need-rel', 'need-edge', 'need-edge-add', 'need-btn', 'need-top', 'need-bar', ...ORDER.map(c => 'need-' + c)];
    document.querySelectorAll('.need').forEach(el => { if (!want.has(el)) { el.classList.remove(...CLS); el.style.removeProperty('--edge-c'); } });
    want.forEach((c, el) => {
      el.classList.remove('need-top', 'need-edge', 'need-edge-add', 'need-btn', 'need-rel');   // вид мог смениться
      ORDER.forEach(o => el.classList.toggle('need-' + o, o === c));
      el.classList.add('need');
      // окошко со своей цветной полоской слева (мойка, срочная деталь) или строка — мигает край, без рамки вокруг
      const bl = parseFloat(getComputedStyle(el).borderLeftWidth) || 0;
      // 23.11.2026 (брат): кнопка, которую надо нажать («Согласовал с клиентом», «Подбор завершён»…) — синий перелив
      if (el.matches('.btn, button, summary.btn')) { el.classList.add('need-btn'); el.style.setProperty('--need-btn-c', btnColor.get(el) || 'var(--need-c)'); return; }
      // 23.11.2026 (брат): цветная плитка — мигает её верхняя полоска, рамки вокруг нет
      if (el.matches('.ptile')) { el.classList.add('need-top'); return; }
      if (bl >= 3 || el.matches(EDGE)) {
        // 23.11.2026 (брат): своя полоска мигает своим цветом (сиреневая «утвердить» — сиреневым), без своей — цветом кружка
        if (bl >= 3 && !el.classList.contains('need-edge')) el.style.setProperty('--edge-c', getComputedStyle(el).borderLeftColor);
        el.classList.add('need-edge');
        if (bl < 3 && !el.matches('tr')) el.classList.add('need-edge-add');
      } else if ((parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0) < 6) {
        // 24.11.2026 (брат): квадратных рамок (прямые углы) нет нигде — у такого окошка / строки мигает полоска слева,
        // как во всей CRM: край скруглённого окошка, концы плавно сужаются (need-edge + need-edge-add)
        el.classList.add('need-edge', 'need-edge-add'); return;
      } else {
        // 25.11.2026 (брат): рамки по контуру нет НИГДЕ. Блок / окошко без своей строки или кнопки (например, блок наряда
        // свёрнут) — мигает его верхняя полоска, как у плиток
        el.classList.add('need-top'); return;
      }
    });
  }
  let last = null;
  window.crmNeed = map => { last = map; apply(map); };
  window.crmNeedAgain = () => { if (last) apply(last); };   // 25.11.2026: свернули / развернули блок — пересчитать
})();

// ===== 23.11.2026 (брат): левое меню не прыгает наверх при переходе на другую страницу =====
// Запоминаем прокрутку меню (и самого меню, и его прокручиваемой части) перед уходом, на новой странице ставим обратно.
(function sideScroll() {
  const KEY = 'crm-side-scroll';
  const boxes = () => [...document.querySelectorAll('.sidebar, .sidebar *')].filter(el => {
    const s = getComputedStyle(el); return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight; });
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || '[]');
    boxes().forEach((el, i) => { if (saved[i]) el.scrollTop = saved[i]; });
  } catch (e) {}
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(boxes().map(el => el.scrollTop))); } catch (e) {} };
  window.addEventListener('pagehide', save);
  document.addEventListener('click', e => { if (e.target.closest('.sidebar a')) save(); }, true);
})();
