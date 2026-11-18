/* ==========================================================================
   AIaaS portal: shared behaviours
   --------------------------------------------------------------------------
   Behaviour hangs on data-* attributes and ARIA roles, so class names stay
   free for styling.

     data-theme-toggle        switch light / dark, saved as "theme"
     role="tab"               tabs (aria-controls points at the panel)
     data-copy="#selector"    copy the text of another element
     data-drawer-open="nav"   open an off-canvas panel ("nav" or "list")
     data-drawer-close        close whichever panel is open
     data-popover-toggle      toggle the [data-popover] named by aria-controls
     data-password-toggle     show / hide the field named by aria-controls
     data-range="output-id"   slider that paints its filled track
     data-autogrow            textarea that grows with its content
     data-chart               usage chart: tooltip and arrow-key travel
     data-status-pill         model status, refreshed from /api/status

   Loaded in <head> so the saved theme is applied before first paint.
   ========================================================================== */
(function () {
  'use strict';

  var root = document.documentElement;

  function read(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function write(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* storage blocked */ }
  }

  function all(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  function announce(message) {
    var live = document.getElementById('live');
    if (live) live.textContent = message;
  }

  function swapIcon(scope, id) {
    var use = scope.querySelector('use');
    if (!use) return null;
    var previous = use.getAttribute('href');
    use.setAttribute('href', id);
    return previous;
  }

  /* ---- Theme ----------------------------------------------------------- */
  var saved = read('theme');
  if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);

  function activeTheme() {
    var chosen = root.getAttribute('data-theme');
    if (chosen === 'light' || chosen === 'dark') return chosen;
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    return dark ? 'dark' : 'light';
  }

  function labelThemeToggles() {
    var next = activeTheme() === 'dark' ? 'light' : 'dark';
    all('[data-theme-toggle]').forEach(function (button) {
      button.setAttribute('aria-label', 'Switch to ' + next + ' theme');
    });
  }

  function toggleTheme() {
    var next = activeTheme() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    write('theme', next);
    labelThemeToggles();
  }

  /* ---- Tabs ------------------------------------------------------------ */
  function selectTab(tab, focus) {
    var list = tab.closest('[role="tablist"]');
    if (!list) return;
    all('[role="tab"]', list).forEach(function (other) {
      var on = other === tab;
      var panel = document.getElementById(other.getAttribute('aria-controls'));
      other.setAttribute('aria-selected', on ? 'true' : 'false');
      other.tabIndex = on ? 0 : -1;
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  }

  function stepTab(tab, key) {
    var tabs = all('[role="tab"]', tab.closest('[role="tablist"]'));
    var index = tabs.indexOf(tab);
    var target = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 }[key];
    if (target === undefined) return false;
    selectTab(tabs[(target + tabs.length) % tabs.length], true);
    return true;
  }

  /* ---- Copy to clipboard ------------------------------------------------ */
  function legacyCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.className = 'sr-only';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); } catch (error) { /* nothing else to try */ }
    document.body.removeChild(area);
  }

  function confirmCopy(button) {
    var label = button.querySelector('[data-copy-label]');
    if (!button.classList.contains('is-copied')) {
      button.dataset.idleIcon = swapIcon(button, '#i-check') || '';
      if (label) { button.dataset.idleLabel = label.textContent; label.textContent = 'Copied'; }
      button.classList.add('is-copied');
    }
    announce('Copied to clipboard');
    window.clearTimeout(button.copyTimer);
    button.copyTimer = window.setTimeout(function () {
      button.classList.remove('is-copied');
      if (button.dataset.idleIcon) swapIcon(button, button.dataset.idleIcon);
      if (label) label.textContent = button.dataset.idleLabel;
    }, 1600);
  }

  function copy(button) {
    var source = document.querySelector(button.getAttribute('data-copy'));
    if (!source) return;
    var text = (source.value !== undefined ? source.value : source.textContent).trim();
    var done = function () { confirmCopy(button); };
    var fallback = function () { legacyCopy(text); done(); };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else {
        fallback();
      }
    } catch (error) {
      fallback();
    }
  }

  /* ---- Off-canvas panels: sidebar ("nav") and conversations ("list") ---- */
  function setDrawer(name, open) {
    var shell = document.getElementById('shell');
    if (!shell) return;
    shell.classList.toggle('is-' + name + '-open', open);
    all('[data-drawer-open="' + name + '"]').forEach(function (button) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  function closeDrawers() {
    setDrawer('nav', false);
    setDrawer('list', false);
  }

  /* ---- Popovers --------------------------------------------------------- */
  function setPopover(button, open) {
    var popover = document.getElementById(button.getAttribute('aria-controls'));
    if (!popover) return;
    popover.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function closePopovers(except) {
    all('[data-popover-toggle][aria-expanded="true"]').forEach(function (button) {
      if (button !== except) setPopover(button, false);
    });
  }

  /* ---- Form helpers ----------------------------------------------------- */
  function togglePassword(button) {
    var field = document.getElementById(button.getAttribute('aria-controls'));
    if (!field) return;
    var reveal = field.type === 'password';
    field.type = reveal ? 'text' : 'password';
    button.setAttribute('aria-pressed', reveal ? 'true' : 'false');
    button.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
    swapIcon(button, reveal ? '#i-eye-off' : '#i-eye');
  }

  function paintRange(range) {
    var min = parseFloat(range.min) || 0;
    var max = parseFloat(range.max) || 100;
    var value = parseFloat(range.value) || 0;
    var output = document.getElementById(range.getAttribute('data-range'));
    range.style.setProperty('--range-fill', ((value - min) / (max - min)) * 100 + '%');
    if (output) output.textContent = value.toFixed(1);
  }

  function grow(area) {
    area.style.height = 'auto';
    area.style.height = area.scrollHeight + 'px';
  }

  /* ---- Usage chart: tooltip and arrow-key travel between the bars ------- */
  function tooltipRow(swatch, name, value) {
    var row = document.createElement('span');
    var key = document.createElement('span');
    var figure = document.createElement('strong');
    row.className = 'chart-tooltip__row';
    key.className = 'swatch swatch--' + swatch;
    figure.textContent = value;
    row.appendChild(key);
    row.appendChild(document.createTextNode(name));
    row.appendChild(figure);
    return row;
  }

  function initChart(chart) {
    var bars = all('[data-day]', chart);
    var tip = document.createElement('div');
    tip.className = 'chart-tooltip';
    tip.hidden = true;
    tip.setAttribute('aria-hidden', 'true');
    chart.appendChild(tip);

    function show(bar) {
      var stack = bar.querySelector('[data-stack]');
      if (!stack) return;
      var title = document.createElement('span');
      title.className = 'chart-tooltip__title';
      title.textContent = bar.dataset.day + ' · ' + bar.dataset.total;
      tip.textContent = '';
      tip.appendChild(title);
      tip.appendChild(tooltipRow('completion', 'Completion', bar.dataset.completion));
      tip.appendChild(tooltipRow('prompt', 'Prompt', bar.dataset.prompt));
      tip.hidden = false;
      var box = chart.getBoundingClientRect();
      var at = stack.getBoundingClientRect();
      var half = tip.offsetWidth / 2;
      var x = at.left + at.width / 2 - box.left;
      tip.style.left = Math.max(half, Math.min(box.width - half, x)) + 'px';
      tip.style.top = at.top - box.top + 'px';
    }

    function hide() { tip.hidden = true; }

    bars.forEach(function (bar, index) {
      bar.addEventListener('pointerenter', function () { show(bar); });
      bar.addEventListener('pointerleave', hide);
      bar.addEventListener('focus', function () { show(bar); });
      bar.addEventListener('blur', hide);
      bar.addEventListener('keydown', function (event) {
        var step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        var next = step && bars[index + step];
        if (!next) return;
        event.preventDefault();
        bar.setAttribute('tabindex', '-1');
        next.setAttribute('tabindex', '0');
        next.focus();
      });
    });
  }


  /* ---- Model status pill: online / offline, from /api/status ------------ */
  var STATUS_TEXT = { online: 'online', offline: 'GPU offline', unknown: 'checking' };

  function paintStatus(state) {
    root.setAttribute('data-model', state);
    all('[data-status-pill]').forEach(function (pill) {
      var label = pill.querySelector('[data-status-label]');
      var prefix = pill.querySelector('[data-status-prefix]');
      pill.classList.toggle('status-pill--online', state === 'online');
      pill.classList.toggle('status-pill--offline', state !== 'online');
      if (prefix) prefix.hidden = state !== 'online';
      if (label) label.textContent = STATUS_TEXT[state] || STATUS_TEXT.unknown;
    });
    document.dispatchEvent(new CustomEvent('aiaas:status', { detail: { state: state } }));
  }

  function refreshStatus() {
    if (!document.querySelector('[data-status-pill]') || document.hidden) return Promise.resolve();
    return fetch('/api/status', { headers: { Accept: 'application/json' } })
      .then(function (response) { return response.ok ? response.json() : { state: 'unknown' }; })
      .then(function (body) { paintStatus(body.state || 'unknown'); })
      .catch(function () { paintStatus('unknown'); });
  }

  window.aiaas = { refreshStatus: refreshStatus, announce: announce, grow: grow };

  /* ---- Wiring ----------------------------------------------------------- */
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var hit;

    if (!target.closest('[data-popover]')) closePopovers(target.closest('[data-popover-toggle]'));

    if (target.closest('[data-theme-toggle]')) return toggleTheme();
    if ((hit = target.closest('[role="tab"]'))) return selectTab(hit);
    if ((hit = target.closest('[data-copy]'))) return copy(hit);
    if ((hit = target.closest('[data-drawer-open]'))) return setDrawer(hit.getAttribute('data-drawer-open'), true);
    if (target.closest('[data-drawer-close]')) return closeDrawers();
    if ((hit = target.closest('[data-popover-toggle]'))) return setPopover(hit, hit.getAttribute('aria-expanded') !== 'true');
    if ((hit = target.closest('[data-password-toggle]'))) return togglePassword(hit);
  });

  document.addEventListener('keydown', function (event) {
    var target = event.target;
    if (event.key === 'Escape') {
      closeDrawers();
      closePopovers();
      return;
    }
    if (target && target.matches && target.matches('[role="tab"]') && stepTab(target, event.key)) {
      event.preventDefault();
    }
  });

  document.addEventListener('input', function (event) {
    var target = event.target;
    if (!target || !target.matches) return;
    if (target.matches('[data-range]')) paintRange(target);
    if (target.matches('[data-autogrow]')) grow(target);
  });

  document.addEventListener('DOMContentLoaded', function () {
    labelThemeToggles();
    all('[data-range]').forEach(paintRange);
    all('[data-chart]').forEach(initChart);
    refreshStatus();
    window.setInterval(refreshStatus, 30000);
    document.addEventListener('visibilitychange', refreshStatus);
  });
})();
