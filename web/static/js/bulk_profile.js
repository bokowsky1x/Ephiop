(() => {
  const form = document.getElementById('bulk-profile-form');
  const checks = [...form.querySelectorAll('.account-check')];
  const all = document.getElementById('select-all');
  const nameMode = document.getElementById('name-mode');
  const aboutMode = document.getElementById('about-mode');
  const applyButton = document.getElementById('apply-button');
  const previewButton = document.getElementById('preview-button');
  const confirmation = document.getElementById('apply-confirmation');
  const error = document.getElementById('bulk-error');
  const progress = document.getElementById('bulk-progress');
  let plan = null;
  let applying = false;
  const selectedIds = () => checks.filter(input => input.checked).map(input => Number(input.value));

  function syncModes() {
    const count = selectedIds().length;
    document.getElementById('selected-count').textContent = count;
    all.checked = count > 0 && count === checks.length;
    all.indeterminate = count > 0 && count < checks.length;
    const sections = {
      'name-fields': nameMode.value === 'ai',
      'about-text-fields': aboutMode.value === 'text',
      'about-ai-fields': aboutMode.value === 'ai',
      'about-link-fields': ['text', 'ai'].includes(aboutMode.value),
    };
    Object.entries(sections).forEach(([id, visible]) => {
      const section = document.getElementById(id);
      section.hidden = !visible;
      section.querySelectorAll('input,textarea').forEach(input => { input.disabled = !visible; });
    });
    previewButton.disabled = count === 0;
  }

  function setBusy(value) {
    form.querySelectorAll('input,select,textarea,button').forEach(input => { input.disabled = value; });
    if (!value) {
      syncModes();
      applyButton.disabled = !plan || !plan.rows.some(row => row.status === 'pending');
    }
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
  }

  async function post(url, data) {
    const response = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...data, csrf_token: form.dataset.csrf}),
    });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || 'Ошибка запроса');
    return result;
  }

  function textBlock(parent, text, className = '') {
    const block = document.createElement('div');
    block.className = 'text-break ' + className;
    block.style.whiteSpace = 'pre-wrap';
    block.textContent = text;
    parent.appendChild(block);
  }

  function profileCell(cell, profile, fields) {
    if (Object.hasOwn(fields, 'first_name')) {
      textBlock(cell, [profile.first_name, profile.last_name].filter(Boolean).join(' '), 'fw-semibold');
    }
    if (Object.hasOwn(fields, 'about')) {
      textBlock(cell, profile.about || '(пусто)', 'small mt-1');
    }
    if (Object.hasOwn(fields, 'personal_channel_id')) {
      textBlock(cell, profile.personal_channel_id
        ? 'Канал: ' + (profile.personal_channel_title || '-100' + profile.personal_channel_id)
        : 'Канал не привязан', 'small mt-1');
    }
  }

  function render() {
    const body = document.getElementById('preview-rows');
    body.replaceChildren();
    plan.rows.forEach(row => {
      const tr = document.createElement('tr');
      const cells = Array.from({length: 4}, () => tr.appendChild(document.createElement('td')));
      cells[1].dataset.label = 'Сейчас';
      cells[2].dataset.label = 'После сохранения';
      textBlock(cells[0], row.label, 'fw-semibold');
      textBlock(cells[0], '#' + row.id, 'small text-muted');
      profileCell(cells[1], row.before, row.changes);
      profileCell(cells[2], row.changes, row.changes);
      textBlock(cells[3], row.message,
        row.status === 'success' ? 'small text-success' :
        ['error', 'uncertain'].includes(row.status) ? 'small text-danger' : 'small text-muted');
      body.appendChild(tr);
    });
    document.getElementById('preview-section').hidden = false;
  }

  function invalidate() {
    plan = null;
    confirmation.hidden = true;
    applyButton.disabled = true;
    document.getElementById('preview-section').hidden = true;
    progress.textContent = '';
    syncModes();
  }
  all.addEventListener('change', () => checks.forEach(input => { input.checked = all.checked; }));
  form.addEventListener('input', event => {
    if (event.target.type !== 'checkbox') invalidate();
  });
  form.addEventListener('change', invalidate);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    error.hidden = true;
    plan = null;
    confirmation.hidden = true;
    document.getElementById('preview-section').hidden = true;
    const data = {
      account_ids: selectedIds(), name_mode: nameMode.value, about_mode: aboutMode.value,
      channel_mode: document.getElementById('channel-mode').value,
      name_prompt: document.getElementById('name-prompt').value,
      about_prompt: document.getElementById('about-prompt').value,
      about_text: document.getElementById('about-text').value,
      about_url: document.getElementById('about-url').value,
      sync_labels: document.getElementById('sync-labels').checked,
    };
    setBusy(true);
    progress.textContent = 'Подготовка предпросмотра';
    try {
      plan = await post(form.dataset.previewUrl, data);
      render();
      progress.textContent = 'Готово к сохранению: ' + plan.rows.filter(row => row.status === 'pending').length;
    } catch (exc) {
      progress.textContent = '';
      showError(exc.message || 'Не удалось получить предпросмотр');
    } finally { setBusy(false); }
  });

  applyButton.addEventListener('click', () => {
    if (!plan) return;
    const count = plan.rows.filter(row => row.status === 'pending').length;
    document.getElementById('confirmation-text').textContent = 'Сохранить изменения в Telegram? Аккаунтов: ' + count;
    confirmation.hidden = false;
  });
  document.getElementById('cancel-apply').addEventListener('click', () => { confirmation.hidden = true; });
  document.getElementById('confirm-apply').addEventListener('click', async () => {
    if (!plan) return;
    const rows = plan.rows.filter(row => row.status === 'pending');
    if (!rows.length || applying) return;
    confirmation.hidden = true;
    applying = true;
    setBusy(true);
    error.hidden = true;
    let completed = 0;
    try {
      for (const row of rows) {
        row.status = 'sending';
        row.message = 'Сохранение';
        render();
        progress.textContent = 'Обработано: ' + completed + ' / ' + rows.length;
        try {
          const result = await post(form.dataset.baseUrl + '/' + plan.plan_id + '/' + row.id + '/apply', {});
          Object.assign(row, result);
          completed++;
          render();
          if (['uncertain', 'sending'].includes(row.status)) {
            showError('Результат не подтверждён. Проверьте этот профиль перед продолжением.');
            break;
          }
        } catch (exc) {
          row.status = 'uncertain';
          row.message = 'Запрос прерван. Проверьте профиль в Telegram';
          render();
          showError(exc.message + '. Автоматического повтора не будет.');
          break;
        }
      }
      progress.textContent = 'Обработано: ' + completed + ' / ' + rows.length;
    } finally {
      applying = false;
      setBusy(false);
      applyButton.disabled = true;
    }
  });
  window.addEventListener('beforeunload', event => {
    if (applying) { event.preventDefault(); event.returnValue = ''; }
  });
  syncModes();
})();
