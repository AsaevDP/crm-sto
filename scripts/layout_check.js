// Замер раскладки (вставляется в браузер): прокрутка вбок, обрезанные кнопки/чипы, таблицы шире рамки, «3E+1».
(() => {
  const out = { w: innerWidth, scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  out.cut = [...document.querySelectorAll('.segmented a, .segmented button, .chip, .btn, .ptile-t, .ptile b')]
    .filter(e => e.offsetParent && e.scrollWidth > e.clientWidth + 1).map(e => e.textContent.trim().slice(0, 40));
  out.wide = [...document.querySelectorAll('table')].filter(t => t.offsetParent && t.parentElement
    && getComputedStyle(t.parentElement).overflowX === 'visible' && t.scrollWidth > t.parentElement.clientWidth + 1)
    .map(t => (t.className || 'table') + ' ' + t.scrollWidth + '>' + t.parentElement.clientWidth);
  out.over = [...document.querySelectorAll('main *')].filter(e => {
    if (!e.offsetParent) return false; const r = e.getBoundingClientRect(); return r.right > innerWidth + 1 && r.width > 0;
  }).slice(0, 8).map(e => e.tagName + '.' + (e.className || '').toString().slice(0, 30));
  out.e = /\d[eE][+-]\d/.test(document.querySelector('main')?.innerText || '');
  return out;
})()
