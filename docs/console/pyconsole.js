/* taken? Pyodide console: a real terminal UI running taken's actual
 * Python code in the visitor's browser. xterm.js handles the display;
 * every `taken ...` line is executed by webshim.run_command() inside
 * Pyodide. This is not a simulation: the checks hit api.github.com live.
 */
(function () {
  var term = new Terminal({
    cols: 80,
    rows: 22,
    cursorBlink: true,
    theme: {
      background: '#0d1117',
      foreground: '#e6edf3',
      cursor: '#58a6ff',
      selectionBackground: 'rgba(88,166,255,0.3)'
    },
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 13
  });
  term.open(document.getElementById('xterm'));

  /* Clickable references: owner/repo#123 opens the GitHub issue, and any
   * full http(s) URL opens directly. xterm 5.x only offers
   * registerLinkProvider (registerLinkMatcher was removed), so matches are
   * computed per line from the buffer. */
  var ISSUE_RE = /([\w][\w.-]*)\/([\w][\w.-]*)#(\d+)/g;
  var ISSUE_FULL_RE = /^([\w][\w.-]*)\/([\w][\w.-]*)#(\d+)$/;
  var URL_RE = /https?:\/\/[^\s]+/g;
  var URL_TRIM_RE = /[.,;:)\]}'"]+$/;
  term.registerLinkProvider({
    provideLinks: function (y, callback) {
      var line = term.buffer.active.getLine(y - 1);
      if (!line) {
        callback(undefined);
        return;
      }
      var text = line.translateToString(true);
      var links = [];
      var match;
      ISSUE_RE.lastIndex = 0;
      while ((match = ISSUE_RE.exec(text)) !== null) {
        links.push({
          range: {
            start: { x: match.index + 1, y: y },
            end: { x: match.index + match[0].length + 1, y: y }
          },
          text: match[0],
          activate: function (event, text) {
            var parts = ISSUE_FULL_RE.exec(text);
            if (parts) {
              window.open(
                'https://github.com/' + parts[1] + '/' + parts[2] + '/issues/' + parts[3],
                '_blank',
                'noopener'
              );
            }
          },
          decorations: { pointerCursor: true, underline: true }
        });
      }
      /* Full http(s) URLs: trim trailing punctuation, then link the URL
       * itself. GitHub issue URLs contain no '#', so this never overlaps
       * the issue-ref matches above. */
      URL_RE.lastIndex = 0;
      while ((match = URL_RE.exec(text)) !== null) {
        var url = match[0].replace(URL_TRIM_RE, '');
        links.push({
          range: {
            start: { x: match.index + 1, y: y },
            end: { x: match.index + url.length + 1, y: y }
          },
          text: url,
          activate: function (event, text) {
            window.open(text, '_blank', 'noopener');
          },
          decorations: { pointerCursor: true, underline: true }
        });
      }
      callback(links.length ? links : undefined);
    }
  });

  var PYODIDE_URL = 'https://cdn.jsdelivr.net/pyodide/v0.27.4/full/';

  function out(text) {
    term.write(String(text).replace(/\n/g, '\r\n') + '\r\n');
  }

  var ready = false;
  var pyodide = null;
  var buf = '';
  var history = [];
  var hIndex = -1;

  function prompt() {
    term.write('\r\n\x1b[1;32m$\x1b[0m ');
  }

  out('loading Python runtime (one-time download, ~15 MB)...');

  loadPyodide({ indexURL: PYODIDE_URL }).then(function (py) {
    pyodide = py;
    return Promise.all([
      fetch('py/checks.py').then(function (r) { return r.text(); }),
      fetch('py/verdict.py').then(function (r) { return r.text(); }),
      fetch('py/webshim.py').then(function (r) { return r.text(); })
    ]);
  }).then(function (texts) {
    pyodide.FS.mkdir('/takenweb');
    pyodide.FS.writeFile('/takenweb/checks.py', texts[0]);
    pyodide.FS.writeFile('/takenweb/verdict.py', texts[1]);
    pyodide.FS.writeFile('/takenweb/webshim.py', texts[2]);
    pyodide.runPython("import sys; sys.path.insert(0, '/takenweb'); import webshim");
    ready = true;
    out("ready. taken's real Python code is running in your browser.");
    out('Type "taken --help". Live checks use GitHub\'s public API (60/hour, no login).');
    prompt();
  }).catch(function (e) {
    out('could not load the Python runtime. Check your connection and reload.');
    out(String((e && e.message) || e));
  });

  function runLine(line) {
    history.push(line);
    hIndex = history.length;
    var cmd = line.trim().toLowerCase();
    if (cmd === 'clear') {
      term.clear();
      prompt();
      return;
    }
    /* Only live commands hit the API (--help/--version/clear are offline). */
    if (!/^(taken(\s+(--help|--version|-h))?\s*|--help|--version)$/.test(cmd)) {
      out('\x1b[2mchecking live via GitHub\'s public API...\x1b[0m');
    }
    /* Yield so the line above paints before the synchronous XHR blocks. */
    setTimeout(function () {
      var result;
      try {
        pyodide.globals.set('__taken_line', line);
        result = pyodide.runPython('webshim.run_command(__taken_line)');
      } catch (e) {
        result = 'error: ' + String((e && e.message) || e);
      }
      out(result);
      prompt();
    }, 30);
  }

  term.onData(function (d) {
    if (!ready) return;
    if (d === '\r') {
      term.write('\r\n');
      var line = buf;
      buf = '';
      runLine(line);
    } else if (d === '\x7f') {
      if (buf.length > 0) {
        buf = buf.slice(0, -1);
        term.write('\b \b');
      }
    } else if (d === '\x03') {
      buf = '';
      term.write('^C');
      prompt();
    } else if (d === '\x1b[A') {
      if (hIndex > 0) {
        hIndex--;
        buf = history[hIndex] || '';
        term.write('\r\x1b[K\x1b[1;32m$\x1b[0m ' + buf);
      }
    } else if (d === '\x1b[B') {
      if (hIndex < history.length - 1) {
        hIndex++;
        buf = history[hIndex];
      } else {
        hIndex = history.length;
        buf = '';
      }
      term.write('\r\x1b[K\x1b[1;32m$\x1b[0m ' + buf);
    } else if (d >= ' ' && d !== '\x7f') {
      buf += d;
      term.write(d);
    }
  });

  document.getElementById('demo-term').addEventListener('click', function () {
    term.focus();
  });

  /* Hint chips fill the input like a real terminal; the visitor presses enter. */
  document.querySelectorAll('.term-hint code').forEach(function (el) {
    el.addEventListener('click', function () {
      if (!ready) return;
      var cmd = el.getAttribute('data-cmd');
      buf = cmd;
      term.write('\r\x1b[K\x1b[1;32m$\x1b[0m ' + cmd);
      term.focus();
    });
  });
})();
