(() => {
  'use strict';

  const element = (name, className = '', text = '') => {
    const node = document.createElement(name);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };

  async function loadJSON(url) {
    const response = await fetch(url, {credentials: 'same-origin'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function renderRevision(revision, versions) {
    const hero = document.querySelector('.legal-hero');
    if (!hero) return;
    const heading = hero.querySelector('h1');
    const summary = hero.querySelector(':scope > p');
    const edition = hero.querySelector('.legal-edition time');
    const pdf = hero.querySelector('.pdf-action') || element('a', 'pdf-action', 'Открыть PDF-редакцию');
    if (heading) heading.textContent = revision.title;
    if (summary) summary.textContent = revision.summary || '';
    if (edition) {
      edition.textContent = revision.version_label;
      edition.dateTime = revision.effective_from;
    }
    pdf.href = `/api/atlas/legal/revisions/${revision.id}/pdf`;
    pdf.target = '_blank';
    pdf.rel = 'noopener';
    if (!pdf.isConnected) hero.insertBefore(pdf, hero.querySelector('.legal-edition'));

    let layout = document.querySelector('.document-layout');
    let documentNode = document.querySelector('.document');
    if (!layout) {
      layout = element('div', 'document-layout');
      const nav = element('aside', 'document-nav');
      nav.append(element('small', '', 'СОДЕРЖАНИЕ'), element('ol'));
      documentNode = element('article', 'document');
      layout.append(nav, documentNode);
      const old = document.querySelector('.contact-stack');
      if (old) old.replaceWith(layout);
      else document.querySelector('.legal-shell')?.append(layout);
    }
    const navList = layout.querySelector('.document-nav ol');
    documentNode.replaceChildren();
    navList?.replaceChildren();
    let sectionIndex = 0;
    let current = null;
    (revision.blocks || []).forEach((block) => {
      if (block.type === 'section' || !current) {
        current = element('section');
        current.id = `revision-section-${++sectionIndex}`;
        const title = block.title || `Раздел ${sectionIndex}`;
        const h2 = element('h2');
        h2.append(element('span', '', String(sectionIndex).padStart(2, '0')), document.createTextNode(title));
        current.append(h2);
        documentNode.append(current);
        if (navList) {
          const li = element('li');
          const link = element('a', '', title);
          link.href = `#${current.id}`;
          li.append(link); navList.append(li);
        }
      } else if (block.title) {
        current.append(element('h3', '', block.title));
      }
      if (block.type === 'list') {
        const list = element('ul');
        (block.items || []).forEach((item) => list.append(element('li', '', item)));
        current.append(list);
      } else if (block.text) {
        const paragraph = element('p', block.type === 'notice' ? 'legal-note' : '', block.text);
        current.append(paragraph);
      }
    });
    const selector = element('label', 'legal-version-picker');
    selector.append(element('span', '', 'Редакция документа'));
    const select = element('select');
    versions.forEach((item) => {
      const option = element('option', '', `${item.version_label}${item.status === 'published' ? ' · действует' : ''}`);
      option.value = String(item.id);
      option.selected = item.id === revision.id;
      select.append(option);
    });
    select.addEventListener('change', () => {
      const selected = versions.find((item) => String(item.id) === select.value);
      if (selected) renderRevision(selected, versions);
    });
    selector.append(select);
    layout.insertAdjacentElement('beforebegin', selector);
    document.querySelectorAll('.legal-version-picker').forEach((node, index) => { if (index) node.remove(); });
  }

  async function hydratePublishedContent() {
    try {
      const manifest = await loadJSON('/api/atlas/billing/content');
      const footer = manifest.slots?.footer_note?.content_text;
      if (footer) document.querySelectorAll('.legal-footer > span').forEach((node) => { node.textContent = footer; });
      const email = manifest.slots?.support_email?.content_text;
      if (email) document.querySelectorAll('a[href^="mailto:"]').forEach((node) => {
        node.href = `mailto:${email}`;
        node.textContent = email;
      });
      const intro = manifest.slots?.legal_center_intro?.content_text;
      if (intro && document.body.dataset.legalIndex) {
        const node = document.querySelector('.legal-hero > p');
        if (node) node.textContent = intro;
      }
    } catch (_) {}
    const documentKey = document.body.dataset.legalDocument;
    if (!documentKey) return;
    try {
      const data = await loadJSON(`/api/atlas/legal/revisions?document=${encodeURIComponent(documentKey)}`);
      const versions = data.revisions || [];
      if (!versions.length) return;
      const current = versions.find((item) => item.id === data.current_revision_id)
        || versions.find((item) => item.status === 'published') || versions[0];
      renderRevision(current, versions);
    } catch (_) {
      // The complete static legal text remains available if the API is down.
    }
  }
  hydratePublishedContent();

  const form = document.querySelector('#privacy-request-form');
  const status = document.querySelector('#privacy-request-status');
  if (!(form instanceof HTMLFormElement) || !(status instanceof HTMLOutputElement)) return;

  let receipt = '';
  const makeReceipt = () => globalThis.crypto?.randomUUID?.()
    || `privacy-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  const show = (message, success = false) => {
    status.textContent = message;
    status.classList.add('visible');
    status.classList.toggle('success', success);
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    receipt ||= makeReceipt();
    show('Регистрируем запрос…', true);
    const values = new FormData(form);
    try {
      const response = await fetch('/api/privacy/requests', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          receipt,
          request_type: values.get('request_type'),
          email: values.get('email'),
          account_login: values.get('account_login'),
          scope: values.get('scope'),
          details: values.get('details'),
          website: values.get('website'),
          acknowledge: values.get('acknowledge') === 'on',
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Запрос не принят (HTTP ${response.status}).`);
      show(`Запрос принят. Номер: ${result.request_code}. Сохраните его.`, true);
      form.querySelectorAll('input, select, textarea, button').forEach((control) => { control.disabled = true; });
    } catch (error) {
      show(error instanceof Error ? error.message : 'Связь прервалась. Повторите отправку.');
      button.disabled = false;
    }
  });
})();
