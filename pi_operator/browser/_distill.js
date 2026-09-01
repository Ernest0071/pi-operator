/*
 * DOM distiller.
 *
 * Runs inside the page. Walks the DOM once and returns a compact, semantically
 * meaningful view of what a human operator could perceive and act on:
 * interactable controls, their accessible names, current values and states,
 * plus structured tables and any surfaced error/alert text.
 *
 * Every returned element is tagged in-place with data-pi-ref, which gives the
 * agent a stable, unambiguous locator for the follow-up action. That tag is the
 * contract between perception and action.
 */
(() => {
  const MAX_ELEMENTS = 220;
  const MAX_NAME = 120;
  const MAX_TABLE_ROWS = 25;

  const INTERACTIVE_TAGS = new Set([
    'A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY', 'OPTION',
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'combobox', 'textbox', 'switch', 'searchbox',
    'slider', 'spinbutton', 'treeitem',
  ]);

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, MAX_NAME);

  function implicitRole(el) {
    const tag = el.tagName;
    switch (tag) {
      case 'A': return el.hasAttribute('href') ? 'link' : 'generic';
      case 'BUTTON': return 'button';
      case 'SELECT': return el.multiple ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'SUMMARY': return 'disclosure';
      case 'IMG': return 'img';
      case 'TABLE': return 'table';
      case 'FORM': return 'form';
      case 'NAV': return 'navigation';
      case 'OPTION': return 'option';
      case 'INPUT': {
        const t = (el.type || 'text').toLowerCase();
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
        if (t === 'file') return 'file-upload';
        if (t === 'search') return 'searchbox';
        if (t === 'range') return 'slider';
        if (t === 'number') return 'spinbutton';
        if (t === 'hidden') return 'hidden';
        return 'textbox';
      }
      default:
        if (/^H[1-6]$/.test(tag)) return 'heading';
        return 'generic';
    }
  }

  function accessibleName(el) {
    // Roughly follows accname resolution order, stopping at "good enough to act on".
    const byIds = el.getAttribute('aria-labelledby');
    if (byIds) {
      const txt = byIds.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.innerText || n.textContent)
        .join(' ');
      if (clean(txt)) return clean(txt);
    }
    const aria = el.getAttribute('aria-label');
    if (clean(aria)) return clean(aria);

    if (el.labels && el.labels.length) {
      const txt = Array.from(el.labels).map((l) => l.innerText || l.textContent).join(' ');
      if (clean(txt)) return clean(txt);
    }
    const wrapping = el.closest('label');
    if (wrapping && clean(wrapping.innerText)) return clean(wrapping.innerText);

    for (const attr of ['placeholder', 'title', 'alt', 'name']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (clean(v)) return clean(v);
    }
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes((el.type || '').toLowerCase())) {
      if (clean(el.value)) return clean(el.value);
    }
    // Text content is only trustworthy for elements whose label IS their text.
    if (['BUTTON', 'A', 'OPTION', 'SUMMARY', 'LI', 'TD', 'TH'].includes(el.tagName)
        || /^H[1-6]$/.test(el.tagName)) {
      return clean(el.innerText || el.textContent);
    }
    // A clickable div in an SPA: its own short text is the only label it has.
    if (window.getComputedStyle(el).cursor === 'pointer') {
      const t = clean(el.innerText || el.textContent);
      if (t && t.length <= 80) return t;
    }
    return '';
  }

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    // Elements scrolled out of view still count: the agent can scroll to them.
    return true;
  }

  function isInteractive(el, role) {
    if (el.disabled) return true;              // reported, but flagged disabled
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (INTERACTIVE_ROLES.has(role)) return true;
    const explicit = (el.getAttribute('role') || '').toLowerCase();
    if (INTERACTIVE_ROLES.has(explicit)) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.isContentEditable) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') return true;
    return false;
  }

  // Single-page apps bind clicks with addEventListener, which leaves no
  // attribute to detect. A pointer cursor is the one reliable trace such
  // elements leave, and without it a whole navigation tree can read as inert.
  const pointerIncluded = new Set();

  function isPointerClickable(el) {
    if (window.getComputedStyle(el).cursor !== 'pointer') return false;
    // Keep the outermost pointer element in a nest so one control yields one
    // entry rather than one per wrapper div.
    for (let a = el.parentElement; a; a = a.parentElement) {
      if (pointerIncluded.has(a)) return false;
    }
    return true;
  }

  function stateOf(el, role) {
    const st = {};
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') st.disabled = true;
    if (el.readOnly) st.readonly = true;
    if (el.required || el.getAttribute('aria-required') === 'true') st.required = true;
    if (role === 'checkbox' || role === 'radio' || role === 'switch') st.checked = !!el.checked;
    const exp = el.getAttribute('aria-expanded');
    if (exp !== null) st.expanded = exp === 'true';
    const sel = el.getAttribute('aria-selected');
    if (sel !== null) st.selected = sel === 'true';
    if (el.getAttribute('aria-invalid') === 'true') st.invalid = true;
    // Native constraint validation blocks submission client-side with no server
    // round-trip and no visible alert, so an agent that only watches for server
    // errors sees "nothing happened". Surface it explicitly.
    if (typeof el.checkValidity === 'function' && !el.checkValidity()) {
      st.invalid = true;
      if (el.validationMessage) st.validation = el.validationMessage;
    }
    if (el.getAttribute('aria-current')) st.current = el.getAttribute('aria-current');
    return st;
  }

  function valueOf(el, role) {
    if (role === 'combobox' || role === 'listbox') {
      const opts = Array.from(el.selectedOptions || []).map((o) => clean(o.textContent));
      return opts.join(', ');
    }
    if (['textbox', 'searchbox', 'spinbutton', 'slider'].includes(role)) {
      return clean(el.value);
    }
    if (el.isContentEditable) return clean(el.innerText);
    return '';
  }

  // Nearest contextual anchor, so the model can disambiguate "Save" among five Saves.
  function contextOf(el) {
    let node = el;
    let hops = 0;
    while (node && hops < 12) {
      node = node.parentElement;
      hops += 1;
      if (!node) break;
      const label = node.getAttribute && (
        node.getAttribute('aria-label') || node.getAttribute('data-section') || ''
      );
      if (clean(label)) return clean(label);
      // Nearest PRECEDING heading, not merely the first one in the subtree:
      // a button after two fieldsets belongs to the second, not the first.
      if (node.querySelectorAll) {
        const headings = Array.from(node.querySelectorAll('h1,h2,h3,legend,caption'))
          .filter((h) => el.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_PRECEDING);
        if (headings.length) {
          const t = clean(headings[headings.length - 1].innerText || headings[headings.length - 1].textContent);
          if (t) return t;
        }
      }
      if (['DIALOG', 'FORM', 'NAV', 'ASIDE', 'HEADER', 'FOOTER', 'MAIN'].includes(node.tagName)) {
        return node.tagName.toLowerCase();
      }
    }
    return '';
  }

  function optionsOf(el, role) {
    if (role !== 'combobox' && role !== 'listbox') return undefined;
    const opts = Array.from(el.options || []).slice(0, 40).map((o) => clean(o.textContent)).filter(Boolean);
    return opts.length ? opts : undefined;
  }

  // --- clear any refs from a previous snapshot so indices never collide ---
  document.querySelectorAll('[data-pi-ref]').forEach((n) => n.removeAttribute('data-pi-ref'));

  const elements = [];
  let counter = 0;

  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
  const candidates = [];
  while (walker.nextNode()) candidates.push(walker.currentNode);

  for (const el of candidates) {
    if (elements.length >= MAX_ELEMENTS) break;
    const role = implicitRole(el);
    if (role === 'hidden') continue;

    const explicitRole = (el.getAttribute('role') || '').toLowerCase();
    const effectiveRole = explicitRole || role;
    let interactive = isInteractive(el, effectiveRole);
    const isHeading = effectiveRole === 'heading';

    if (!interactive && !isHeading) {
      if (!isVisible(el) || !isPointerClickable(el)) continue;
      interactive = true;
      pointerIncluded.add(el);
    }
    if (!isVisible(el)) continue;

    const name = accessibleName(el);
    // A nameless, valueless control is noise unless it is an obvious action target.
    const value = valueOf(el, effectiveRole);
    if (!name && !value && !['button', 'link', 'checkbox', 'radio'].includes(effectiveRole)) continue;

    const ref = `e${counter++}`;
    el.setAttribute('data-pi-ref', ref);
    const r = el.getBoundingClientRect();

    elements.push({
      ref,
      role: effectiveRole,
      name,
      value,
      state: stateOf(el, effectiveRole),
      options: optionsOf(el, effectiveRole),
      context: contextOf(el),
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      inViewport: r.top < window.innerHeight && r.bottom > 0,
    });
  }

  // --- tables: structure preserved, not flattened to soup ---
  const tables = [];
  document.querySelectorAll('table').forEach((t, i) => {
    if (tables.length >= 4) return;
    if (!isVisible(t)) return;
    const headers = Array.from(t.querySelectorAll('thead th, tr:first-child th'))
      .map((h) => clean(h.innerText)).filter(Boolean);
    const rows = Array.from(t.querySelectorAll('tbody tr')).slice(0, MAX_TABLE_ROWS).map((tr) =>
      Array.from(tr.querySelectorAll('td, th')).map((td) => clean(td.innerText))
    );
    if (!headers.length && !rows.length) return;
    const ref = t.getAttribute('data-pi-ref') || `t${i}`;
    t.setAttribute('data-pi-ref', ref);
    tables.push({
      ref,
      caption: clean((t.querySelector('caption') || {}).innerText || ''),
      headers,
      rows,
      truncated: t.querySelectorAll('tbody tr').length > MAX_TABLE_ROWS,
    });
  });

  // --- readable content ---
  // Interactables tell an operator what it can DO; for an analytics task the
  // numbers on the page are the point. Collect leaf text, skipping anything
  // already reported as an element label.
  const text = [];
  const seenText = new Set(elements.map((e) => e.name).filter(Boolean));
  const leaves = document.querySelectorAll('p, span, td, th, li, h1, h2, h3, h4, h5, dt, dd, label, div');
  for (const el of leaves) {
    if (text.length >= 160) break;
    if (el.children.length) continue;                 // leaves only
    if (!isVisible(el)) continue;
    const t = clean(el.innerText || el.textContent);
    if (!t || t.length > 200 || seenText.has(t)) continue;
    seenText.add(t);
    text.push(t);
  }

  // --- anything the page is shouting about ---
  const alerts = [];
  const alertSel = '[role="alert"], [aria-live="assertive"], [aria-live="polite"], .error, .alert-danger, .invalid-feedback, .text-danger';
  document.querySelectorAll(alertSel).forEach((n) => {
    if (alerts.length >= 8) return;
    if (!isVisible(n)) return;
    const t = clean(n.innerText || n.textContent);
    if (t && !alerts.includes(t)) alerts.push(t);
  });

  // A blocked native submit is the single most confusing failure for an agent:
  // the click "worked" and the page simply did not change. Name it up front.
  elements.forEach((e) => {
    if (e.state && e.state.validation) {
      const msg = `${e.name || e.role}: ${e.state.validation}`;
      if (!alerts.includes(msg)) alerts.push(msg);
    }
  });

  return {
    url: location.href,
    title: document.title,
    elements,
    tables,
    alerts,
    text,
    truncated: elements.length >= MAX_ELEMENTS,
    scroll: {
      y: Math.round(window.scrollY),
      height: Math.round(document.documentElement.scrollHeight),
      viewport: Math.round(window.innerHeight),
    },
  };
})()
