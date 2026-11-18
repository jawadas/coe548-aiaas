/* ==========================================================================
   AIaaS portal: the chat playground
   --------------------------------------------------------------------------
   Conversations live in this browser (localStorage); the server only relays
   each request to the model and streams the reply back (POST /api/chat).
   Model output is markdown: it is rendered with marked and always passed
   through DOMPurify before it touches the page.
   ========================================================================== */
(function () {
  'use strict';

  var root = document.querySelector('[data-chat]');
  if (!root) return;

  var STORE = 'aiaas.chats.v1';
  var SETTINGS = 'aiaas.chat.settings.v1';
  var MAX_CHATS = 30;
  var DEFAULTS = { temperature: 0.7, max_tokens: 2048, reasoning_effort: 'medium', system: '' };
  var SVG = 'http://www.w3.org/2000/svg';

  var $ = function (selector) { return root.querySelector(selector); };
  var thread = $('[data-thread]');
  var threadInner = $('[data-thread-inner]');
  var emptyState = $('[data-empty]');
  var offlineState = $('[data-offline]');
  var composer = $('[data-composer]');
  var input = $('[data-input]');
  var sendButton = $('[data-send]');
  var stopButton = $('[data-stop]');
  var notice = $('[data-chat-notice]');
  var noticeText = $('[data-chat-notice-text]');
  var titleEl = $('[data-chat-title]');
  var listNav = $('[data-chat-list]');
  var listItems = $('[data-chat-items]');
  var listEmpty = $('[data-chat-list-empty]');
  var temperature = document.getElementById('set-temperature');
  var maxTokens = document.getElementById('set-max-tokens');
  var systemPrompt = document.getElementById('set-system');
  var placeholder = input.placeholder;

  var chats = load(STORE, []);
  var current = null;      // the conversation on screen
  var controller = null;   // AbortController of the reply being streamed
  var offline = false;
  var codeCounter = 0;

  /* ---- Storage ---------------------------------------------------------- */
  function load(key, fallback) {
    try { return JSON.parse(window.localStorage.getItem(key)) || fallback; } catch (error) { return fallback; }
  }

  function save() {
    chats.sort(function (a, b) { return b.updated - a.updated; });
    chats = chats.filter(function (chat) { return chat.messages.length; }).slice(0, MAX_CHATS);
    try { window.localStorage.setItem(STORE, JSON.stringify(chats)); } catch (error) { /* storage full or blocked */ }
  }

  function readSettings() {
    var saved = load(SETTINGS, {});
    var effort = document.querySelector('input[name="set-effort"]:checked');
    return {
      temperature: parseFloat(temperature.value),
      max_tokens: Math.min(8192, Math.max(16, parseInt(maxTokens.value, 10) || DEFAULTS.max_tokens)),
      reasoning_effort: effort ? effort.value : saved.reasoning_effort || DEFAULTS.reasoning_effort,
      system: systemPrompt.value.trim(),
    };
  }

  function applySettings(values) {
    temperature.value = values.temperature;
    maxTokens.value = values.max_tokens;
    systemPrompt.value = values.system;
    var effort = document.getElementById('set-effort-' + values.reasoning_effort);
    if (effort) effort.checked = true;
    temperature.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function storeSettings() {
    try { window.localStorage.setItem(SETTINGS, JSON.stringify(readSettings())); } catch (error) { /* ignore */ }
  }

  /* ---- Small DOM helpers ------------------------------------------------ */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(id, className) {
    var svg = document.createElementNS(SVG, 'svg');
    var use = document.createElementNS(SVG, 'use');
    svg.setAttribute('class', 'icon' + (className ? ' ' + className : ''));
    svg.setAttribute('aria-hidden', 'true');
    use.setAttribute('href', '#' + id);
    svg.appendChild(use);
    return svg;
  }

  function iconButton(id, label) {
    var button = el('button', 'btn btn--ghost btn--icon btn--sm');
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.appendChild(icon(id, 'icon--sm'));
    return button;
  }

  /* ---- Markdown, always sanitised --------------------------------------- */
  function renderMarkdown(target, text) {
    var html = window.marked.parse(text || '', { gfm: true, breaks: true });
    var clean = window.DOMPurify.sanitize(html, {
      FORBID_TAGS: ['style', 'img', 'picture', 'video', 'audio', 'iframe', 'form', 'input', 'button', 'textarea', 'select', 'svg', 'math'],
      FORBID_ATTR: ['style', 'id', 'name', 'srcset'],
    });
    target.innerHTML = clean;  // safe: DOMPurify output only

    target.querySelectorAll('*').forEach(function (node) {
      var language = node.tagName === 'CODE' ? /(?:^|\s)(language-[\w+#.-]+)/.exec(node.className) : null;
      node.removeAttribute('class');
      if (language) node.className = language[1];
    });
    target.querySelectorAll('a').forEach(function (link) {
      link.target = '_blank';
      link.rel = 'noopener noreferrer nofollow';
    });
    target.querySelectorAll('table').forEach(function (table) {
      var wrap = el('div', 'table-wrap');
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    });
    target.querySelectorAll('pre > code').forEach(function (code) {
      var pre = code.parentNode;
      var box = el('div', 'code');
      var header = el('div', 'code__header');
      var language = (/language-([\w+#.-]+)/.exec(code.className) || [])[1] || 'text';
      var copy = el('button', 'btn btn--ghost btn--sm');
      code.id = 'chat-code-' + (codeCounter += 1);
      copy.type = 'button';
      copy.setAttribute('data-copy', '#' + code.id);
      copy.appendChild(icon('i-copy', 'icon--sm'));
      var label = el('span', '', 'Copy');
      label.setAttribute('data-copy-label', '');
      copy.appendChild(label);
      header.appendChild(el('span', 'code__lang', language));
      header.appendChild(copy);
      box.dir = 'ltr';
      pre.parentNode.insertBefore(box, pre);
      box.appendChild(header);
      box.appendChild(pre);
      pre.tabIndex = 0;
      if (window.hljs && window.hljs.getLanguage(language)) {
        try { window.hljs.highlightElement(code); } catch (error) { /* leave it plain */ }
      }
      code.classList.add('hljs');
    });
  }

  /* ---- Messages --------------------------------------------------------- */
  function statsText(message) {
    var parts = [];
    var stats = message.stats || {};
    if (stats.tokens) parts.push(stats.tokens + ' tokens');
    if (stats.seconds) parts.push(stats.seconds.toFixed(1) + 's');
    if (stats.rate) parts.push(Math.round(stats.rate) + ' tok/s');
    if (message.stopped) parts.push('stopped');
    return parts.join(' · ');
  }

  function buildUser(message) {
    var article = el('article', 'chat-msg chat-msg--user');
    var bubble = el('div', 'chat-msg__bubble');
    var body = el('div', 'chat-msg__body', message.content);
    article.setAttribute('aria-label', 'You');
    body.dir = 'auto';
    bubble.appendChild(body);
    article.appendChild(bubble);
    return article;
  }

  function buildAssistant(message, isLast) {
    var article = el('article', 'chat-msg chat-msg--assistant');
    var avatar = icon('logo');
    var content = el('div', 'chat-msg__content');
    article.setAttribute('aria-label', root.getAttribute('data-model'));
    avatar.setAttribute('class', 'chat-msg__avatar');
    article.appendChild(avatar);
    article.appendChild(content);
    article.view = { content: content, isLast: isLast };
    paintAssistant(article, message);
    return article;
  }

  /* Draw (or redraw, while streaming) one assistant message. */
  function paintAssistant(article, message) {
    var view = article.view;
    var streaming = !!message.streaming;
    article.classList.toggle('chat-msg--streaming', streaming);
    article.classList.toggle('chat-msg--error', !!message.error);
    if (streaming) article.setAttribute('aria-busy', 'true'); else article.removeAttribute('aria-busy');

    if (message.reasoning && !view.reasoning) {
      var details = el('details', 'reasoning');
      var summary = el('summary', 'reasoning__summary');
      view.reasoningLabel = document.createTextNode('');
      view.reasoningLead = el('span');
      summary.appendChild(view.reasoningLead);
      summary.appendChild(view.reasoningLabel);
      summary.appendChild(icon('i-chevron-down', 'icon--xs reasoning__chevron'));
      view.reasoningBody = el('div', 'reasoning__body');
      view.reasoningBody.dir = 'auto';
      details.appendChild(summary);
      details.appendChild(view.reasoningBody);
      view.content.insertBefore(details, view.content.firstChild);
      view.reasoning = details;
    }
    if (view.reasoning) {
      var thinking = streaming && !message.content;
      view.reasoningLead.textContent = '';
      view.reasoningLead.appendChild(thinking ? el('span', 'spinner') : icon('i-sparkles', 'icon--sm reasoning__icon'));
      view.reasoningLabel.textContent = thinking
        ? ' Thinking… '
        : ' Thought' + (message.thoughtSeconds ? ' for ' + Math.max(1, Math.round(message.thoughtSeconds)) + 's ' : ' ');
      view.reasoningBody.textContent = message.reasoning;
    }

    if (!view.body) {
      view.body = el('div', 'chat-msg__body prose');
      view.body.dir = 'auto';
      view.content.appendChild(view.body);
    }
    if (message.error) {
      view.body.textContent = '';
      var alert = el('div', 'alert alert--' + (message.error.code === 'model_offline' ? 'info' : 'warning'));
      alert.appendChild(icon(message.error.code === 'model_offline' ? 'i-info' : 'i-warning', 'alert__icon'));
      alert.appendChild(el('p', 'alert__text', message.error.message));
      view.body.appendChild(alert);
    } else {
      renderMarkdown(view.body, message.content);
    }

    if (!streaming && !view.meta) {
      view.meta = el('footer', 'chat-msg__meta');
      view.stats = el('span', 'chat-msg__stats');
      view.meta.appendChild(view.stats);
      if (message.content) {
        var copy = iconButton('i-copy', 'Copy answer');
        copy.addEventListener('click', function () {
          if (navigator.clipboard) navigator.clipboard.writeText(message.content);
          window.aiaas.announce('Copied to clipboard');
        });
        view.meta.appendChild(copy);
      }
      if (view.isLast) {
        var again = iconButton('i-refresh', message.error ? 'Try again' : 'Regenerate answer');
        again.addEventListener('click', regenerate);
        view.meta.appendChild(again);
      }
      view.content.appendChild(view.meta);
    }
    if (view.stats) view.stats.textContent = statsText(message);
  }

  /* ---- Page state ------------------------------------------------------- */
  function nearBottom() {
    return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  }

  function scrollDown() {
    thread.scrollTop = thread.scrollHeight;
  }

  function renderThread() {
    var messages = current ? current.messages : [];
    threadInner.textContent = '';
    messages.forEach(function (message, index) {
      var last = index === messages.length - 1;
      threadInner.appendChild(message.role === 'user' ? buildUser(message) : buildAssistant(message, last));
    });
    titleEl.textContent = current && current.title ? current.title : 'New chat';
    document.title = (current && current.title ? current.title : 'Playground') + ' · AIaaS';
    paintState();
    scrollDown();
  }

  function paintState() {
    var has = !!(current && current.messages.length);
    thread.hidden = !has;
    offlineState.hidden = has || !offline;
    emptyState.hidden = has || offline;
    composer.classList.toggle('is-disabled', offline);
    input.disabled = offline;
    sendButton.disabled = offline;
    input.placeholder = offline ? 'Messages are paused until the GPU wakes up' : placeholder;
    sendButton.hidden = !!controller;
    stopButton.hidden = !controller;
  }

  function renderList() {
    var saved = chats.filter(function (chat) { return chat.messages.length; });
    listItems.textContent = '';
    saved.forEach(function (chat) {
      var row = el('li', 'chat-list__row');
      var link = el('a', 'chat-list__item');
      var title = el('span', 'chat-list__title', chat.title || 'New chat');
      var remove = iconButton('i-trash', 'Delete ' + (chat.title || 'chat'));
      title.dir = 'auto';
      link.href = '#' + chat.id;
      if (current && chat.id === current.id) link.setAttribute('aria-current', 'page');
      link.appendChild(title);
      link.addEventListener('click', function (event) {
        event.preventDefault();
        open(chat);
      });
      remove.classList.add('chat-list__more');
      remove.addEventListener('click', function () {
        if (!window.confirm('Delete this chat? It only exists in this browser.')) return;
        chats = chats.filter(function (other) { return other !== chat; });
        save();
        if (current === chat) newChat(); else renderList();
      });
      row.appendChild(link);
      row.appendChild(remove);
      listItems.appendChild(row);
    });
    listNav.hidden = !saved.length;
    listEmpty.hidden = !!saved.length;
  }

  function open(chat) {
    stop();
    current = chat;
    hideNotice();
    renderThread();
    renderList();
    document.getElementById('shell').classList.remove('is-list-open');
  }

  function newChat() {
    open({ id: 'c' + Date.now().toString(36), title: '', updated: Date.now(), messages: [] });
    input.focus();
  }

  function showNotice(text) {
    noticeText.textContent = text;
    notice.hidden = false;
  }

  function hideNotice() {
    notice.hidden = true;
  }

  /* ---- Talking to the model --------------------------------------------- */
  function history() {
    var settings = readSettings();
    var messages = settings.system ? [{ role: 'system', content: settings.system }] : [];
    current.messages.forEach(function (message) {
      if (message.error || !message.content) return;
      messages.push({ role: message.role, content: message.content });
    });
    return { messages: messages.slice(-60), settings: settings };
  }

  function stop() {
    if (controller) controller.abort();
  }

  function send(text) {
    text = (text || '').trim();
    if (!text || controller || offline) return;
    if (!current) newChat();
    if (!current.messages.length) {
      current.title = text.replace(/\s+/g, ' ').slice(0, 60);
      chats.unshift(current);
    }
    current.messages.push({ role: 'user', content: text });
    input.value = '';
    window.aiaas.grow(input);
    reply();
  }

  function regenerate() {
    if (controller || !current) return;
    while (current.messages.length && current.messages[current.messages.length - 1].role === 'assistant') {
      current.messages.pop();
    }
    if (current.messages.length) reply();
  }

  function reply() {
    var request = history();
    var message = { role: 'assistant', content: '', reasoning: '', streaming: true };
    var started = performance.now();
    var firstContent = 0;
    var firstReasoning = 0;
    var usage = null;
    var frame = 0;
    var article;

    hideNotice();
    current.messages.push(message);
    current.updated = Date.now();
    controller = new AbortController();
    renderThread();
    renderList();
    article = threadInner.lastElementChild;

    function repaint() {
      if (frame) return;
      frame = window.requestAnimationFrame(function () {
        var follow = nearBottom();
        frame = 0;
        paintAssistant(article, message);
        if (follow) scrollDown();
      });
    }

    function handle(event) {
      var now = performance.now();
      if (event.type === 'reasoning') {
        if (!firstReasoning) firstReasoning = now;
        message.reasoning += event.delta;
      } else if (event.type === 'content') {
        if (!firstContent) {
          firstContent = now;
          if (firstReasoning) message.thoughtSeconds = (now - firstReasoning) / 1000;
        }
        message.content += event.delta;
      } else if (event.type === 'usage') {
        usage = event;
      } else if (event.type === 'error') {
        message.error = { code: event.code, message: event.message };
        if (event.code === 'rate_limited') {
          showNotice(event.retry_after
            ? 'You have reached your rate limit. Try again in about ' + event.retry_after + ' seconds.'
            : event.message);
        }
        if (event.code === 'model_offline') window.aiaas.refreshStatus();
      }
      repaint();
    }

    function finish(aborted) {
      var seconds = (performance.now() - started) / 1000;
      var tokens = usage && usage.completion_tokens;
      var writing = firstReasoning || firstContent;  // the first token of any kind
      if (frame) { window.cancelAnimationFrame(frame); frame = 0; }
      message.streaming = false;
      message.stopped = aborted || undefined;
      message.stats = { tokens: tokens || 0, seconds: seconds };
      if (tokens && writing) message.stats.rate = tokens / Math.max(0.2, (performance.now() - writing) / 1000);
      if (!message.content && !message.error) {
        if (aborted) current.messages.pop();
        else message.error = { code: 'gateway_error', message: 'The model sent no answer. Try again.' };
      }
      controller = null;
      current.updated = Date.now();
      save();
      renderThread();
      renderList();
      if (!offline) input.focus();
    }

    fetch('/api/chat', {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({
        messages: request.messages,
        temperature: request.settings.temperature,
        max_tokens: request.settings.max_tokens,
        reasoning_effort: request.settings.reasoning_effort,
      }),
    }).then(function (response) {
      if (response.status === 401) {
        window.location.href = '/login?next=/chat';
        return null;
      }
      if (!response.ok || !response.body) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          var error = body.error || {};
          handle({ type: 'error', code: error.code || 'gateway_error', message: error.message || 'Something went wrong. Try again.' });
        });
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      function pump() {
        return reader.read().then(function (result) {
          if (result.done) return null;
          buffer += decoder.decode(result.value, { stream: true });
          var blocks = buffer.split('\n\n');
          buffer = blocks.pop();
          blocks.forEach(function (block) {
            block.split('\n').forEach(function (line) {
              if (line.indexOf('data: ') !== 0) return;
              try { handle(JSON.parse(line.slice(6))); } catch (error) { /* skip a malformed line */ }
            });
          });
          return pump();
        });
      }
      return pump();
    }).then(function () {
      finish(false);
    }).catch(function (error) {
      var aborted = error && error.name === 'AbortError';
      if (!aborted) handle({ type: 'error', code: 'gateway_error', message: 'The connection was lost. Try again.' });
      finish(aborted);
    });
  }

  /* ---- Wiring ----------------------------------------------------------- */
  composer.addEventListener('submit', function (event) {
    event.preventDefault();
    send(input.value);
  });

  input.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    send(input.value);
  });

  stopButton.addEventListener('click', stop);

  root.addEventListener('click', function (event) {
    var hit = event.target.closest('[data-suggestion], [data-new-chat], [data-check-again], [data-settings-reset]');
    if (!hit) return;
    if (hit.hasAttribute('data-suggestion')) return send(hit.getAttribute('data-suggestion'));
    if (hit.hasAttribute('data-new-chat')) return newChat();
    if (hit.hasAttribute('data-settings-reset')) {
      applySettings(DEFAULTS);
      return storeSettings();
    }
    var note = $('[data-last-checked]');
    hit.classList.add('is-loading');
    note.textContent = 'Checking…';
    window.aiaas.refreshStatus().then(function () {
      hit.classList.remove('is-loading');
      note.textContent = 'Checked just now';
    });
  });

  $('#chat-settings').addEventListener('change', storeSettings);

  document.addEventListener('aiaas:status', function (event) {
    var nowOffline = event.detail.state !== 'online';
    if (nowOffline === offline) return;
    offline = nowOffline;
    paintState();
  });

  applySettings(Object.assign({}, DEFAULTS, load(SETTINGS, {})));
  newChat();
})();
