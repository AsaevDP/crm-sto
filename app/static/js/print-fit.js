/* Печатные листы (акты, отчёты, ведомости — 12.11.2026): на телефоне лист A4 шире экрана.
   Уменьшаем лист целиком под ширину экрана (как предпросмотр документа). На печать не влияет. */
(function () {
  function fit() {
    var sheets = document.querySelectorAll('.sheet');
    if (!sheets.length) return;
    document.body.style.zoom = '';
    var w = 0;
    sheets.forEach(function (s) { w = Math.max(w, s.offsetWidth); });
    var room = document.documentElement.clientWidth - 16;
    document.body.style.zoom = w > room ? String(room / w) : '';
  }
  window.addEventListener('load', fit);
  window.addEventListener('resize', fit);
  window.addEventListener('beforeprint', function () { document.body.style.zoom = ''; });
  window.addEventListener('afterprint', fit);
})();
