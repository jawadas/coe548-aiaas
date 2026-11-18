/* Colour the code samples on the docs page. highlight.js only adds classes, so this is CSP-safe. */
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('pre > code[class*="language-"]').forEach(function (code) {
    var language = (/language-([\w-]+)/.exec(code.className) || [])[1];
    if (window.hljs && window.hljs.getLanguage(language)) window.hljs.highlightElement(code);
    code.classList.add('hljs');
  });
});
