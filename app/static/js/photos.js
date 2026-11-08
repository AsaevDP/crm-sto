// Фото: сжатие на телефоне перед загрузкой + просмотр на весь экран.
// Без внешних библиотек. Сжимаем до ~1600px JPEG 80% (экономия Wi-Fi и диска) и делаем миниатюру 400px.
(function () {
  async function loadImage(file) {
    // createImageBitmap учитывает поворот из EXIF (фото с iPhone не лягут «на бок»)
    if (window.createImageBitmap) {
      try { return await createImageBitmap(file, { imageOrientation: 'from-image' }); } catch (e) {}
    }
    return new Promise((ok, fail) => {
      const img = new Image();
      img.onload = () => ok(img);
      img.onerror = fail;
      img.src = URL.createObjectURL(file);
    });
  }

  function toBlob(img, max, quality) {
    const w = img.width, h = img.height, k = Math.min(1, max / Math.max(w, h));
    const c = document.createElement('canvas');
    c.width = Math.round(w * k); c.height = Math.round(h * k);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    return new Promise(ok => c.toBlob(ok, 'image/jpeg', quality));
  }

  async function compress(file) {
    const img = await loadImage(file);
    const [full, thumb] = await Promise.all([toBlob(img, 1600, 0.8), toBlob(img, 400, 0.7)]);
    return { full, thumb, preview: URL.createObjectURL(thumb) };
  }

  /** Выбор фото: input[type=file] → сжатые фото в массиве, превью в контейнере. */
  window.PhotoPicker = function (input, grid, max, onChange) {
    const items = [];
    function render() {
      grid.innerHTML = '';
      items.forEach((it, i) => {
        const d = document.createElement('div');
        d.className = 'pp-item';
        d.innerHTML = `<img src="${it.preview}" alt=""><button type="button" aria-label="Убрать фото">✕</button>`;
        d.querySelector('button').onclick = () => { items.splice(i, 1); render(); };
        grid.appendChild(d);
      });
      onChange && onChange(items.length);
    }
    input.addEventListener('change', async () => {
      const files = [...input.files].slice(0, Math.max(0, max - items.length));
      input.value = '';
      grid.classList.add('busy');
      for (const f of files) {
        try { items.push(await compress(f)); render(); }
        catch (e) { alert('Не удалось прочитать фото: ' + f.name); }
      }
      grid.classList.remove('busy');
    });
    return {
      items,
      appendTo(fd) { items.forEach((it, i) => { fd.append('photos', it.full, `p${i}.jpg`); fd.append('thumbs', it.thumb, `t${i}.jpg`); }); },
    };
  };

  window.compressPhoto = compress;

  // Формы с фото (инструмент, проверки): отметить выбранное фото и сжать перед отправкой
  document.querySelectorAll('input[type=file][accept^="image"]').forEach(inp => inp.addEventListener('change', () => {
    const lab = inp.closest('label');
    if (lab && inp.files.length && !lab.querySelector('.picked')) lab.insertAdjacentHTML('beforeend', '<span class="picked"> ✓</span>');
  }));
  document.querySelectorAll('form[data-compress], form.check-form').forEach(form => form.addEventListener('submit', async e => {
    const files = [...form.querySelectorAll('input[type=file]')].filter(i => i.files.length);
    if (!files.length) return;           // фото нет — обычная отправка
    e.preventDefault();
    const fd = new FormData(form);
    if (e.submitter && e.submitter.name) fd.set(e.submitter.name, e.submitter.value);
    for (const inp of files) {
      fd.delete(inp.name);
      for (const [n, file] of [...inp.files].entries()) {
        try { const c = await compress(file); fd.append(inp.name, c.full, `photo${n}.jpg`); } catch (err) { fd.append(inp.name, file); }
      }
    }
    const r = await fetch(form.action, { method: 'POST', body: fd, credentials: 'same-origin' });
    location.href = r.url || location.href;
  }));

  // ---- Просмотр на весь экран: <a data-lightbox href="полное фото"> ----
  const links = [...document.querySelectorAll('a[data-lightbox]')];
  if (!links.length) return;
  const box = document.createElement('div');
  box.className = 'lightbox';
  box.innerHTML = '<img alt=""><button class="lb-close" aria-label="Закрыть">✕</button>' +
    '<button class="lb-prev" aria-label="Назад">‹</button><button class="lb-next" aria-label="Вперёд">›</button><div class="lb-count"></div>';
  document.body.appendChild(box);
  const img = box.querySelector('img'), count = box.querySelector('.lb-count');
  let cur = 0;
  function show(i) {
    cur = (i + links.length) % links.length;
    img.src = links[cur].href;
    count.textContent = `${cur + 1} из ${links.length}`;
    box.classList.add('on');
  }
  links.forEach((a, i) => a.addEventListener('click', e => { e.preventDefault(); show(i); }));
  box.querySelector('.lb-close').onclick = () => box.classList.remove('on');
  box.querySelector('.lb-prev').onclick = () => show(cur - 1);
  box.querySelector('.lb-next').onclick = () => show(cur + 1);
  box.addEventListener('click', e => { if (e.target === box) box.classList.remove('on'); });
  document.addEventListener('keydown', e => {
    if (!box.classList.contains('on')) return;
    if (e.key === 'Escape') box.classList.remove('on');
    if (e.key === 'ArrowLeft') show(cur - 1);
    if (e.key === 'ArrowRight') show(cur + 1);
  });
  // Свайп на iPhone
  let x0 = null;
  box.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; }, { passive: true });
  box.addEventListener('touchend', e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 50) show(cur + (dx < 0 ? 1 : -1));
    x0 = null;
  });
})();
