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
})();
