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
  function remember(f) {
    if ((f.method || '').toLowerCase() !== 'post' || (f.target && f.target !== '_self')) return;
    try {
      sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ p: location.pathname, y: window.scrollY, t: Date.now(),
        cols: scrollers().map(c => c.scrollTop) }));
    } catch (err) {}
  }
  document.addEventListener('submit', e => {
    if (e.target instanceof HTMLFormElement && !e.defaultPrevented) remember(e.target);
  });
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

  // ===== Уведомления в углу экрана: исчезают ТОЛЬКО по крестику =====
  // Открытые окошки хранятся в браузере и показываются снова на каждой странице, пока их не закроют.
  const KEY = 'crm-toasts';
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; } }
  function save(list) { try { localStorage.setItem(KEY, JSON.stringify(list.slice(-20))); } catch (e) {} }
  function stack() {
    let s = document.querySelector('.toasts');
    if (!s) { s = document.createElement('div'); s.className = 'toasts'; s.setAttribute('aria-live', 'polite'); document.body.appendChild(s); }
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
      save(load().filter(x => x.id !== t.id)); el.remove();
      if (t.readUrl) fetch(t.readUrl, { method: 'POST', headers: { 'x-requested-with': 'fetch' } }).catch(() => {});
    };
    close.addEventListener('click', done);
    el.append(body, close);
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
    stack().appendChild(el);
  }
  window.crmToast = function (t) {
    if (!t || !t.id) return;
    const list = load();
    if (!list.some(x => x.id === t.id)) { list.push(t); save(list); }
    render(t);
  };
  load().forEach(render);
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
