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
    const close = document.createElement('button');
    close.className = 'toast-x'; close.type = 'button'; close.title = 'Закрыть'; close.setAttribute('aria-label', 'Закрыть'); close.textContent = '×';
    close.addEventListener('click', () => { save(load().filter(x => x.id !== t.id)); el.remove(); });
    el.append(body, close); stack().appendChild(el);
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