/* QR-код для страницы настройки «Штурмана». Написан для этого проекта, сторонний код не взят.
 *
 * Зачем свой: страница не обращается к чужим серверам и не тянет библиотек, а ссылка входа
 * в аккаунт Telegram (tg://login?token=…) — секрет на полминуты, и рисовать её должен браузер
 * владельца, а не чей-то сервис.
 *
 * Что умеет: байтовый режим, уровень коррекции M, версии 1–10 — до 213 байт. Ссылка входа
 * Telegram занимает около 60 байт, ссылка привязки к боту — около 80. По стандарту ISO/IEC 18004.
 * Проверяется тестом service/tests/test_setup_qr.py: матрицы сверяются с библиотекой segno
 * (она же рисует QR в терминале для `shturman tg-login`).
 */
(function (root) {
  "use strict";

  /* Версия → [всего кодовых слов, слов коррекции в блоке, [[блоков, слов данных в блоке], …]]. Уровень M. */
  var VERSIONS = [null,
    [26, 10, [[1, 16]]], [44, 16, [[1, 28]]], [70, 26, [[1, 44]]], [100, 18, [[2, 32]]],
    [134, 24, [[2, 43]]], [172, 16, [[4, 27]]], [196, 18, [[4, 31]]],
    [242, 22, [[2, 38], [2, 39]]], [292, 22, [[3, 36], [2, 37]]], [346, 26, [[4, 43], [1, 44]]]];
  /* Версия → координаты центров выравнивающих узоров. */
  var ALIGN = [null, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
    [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]];
  var MAX_VERSION = 10;

  /* --- арифметика поля GF(256), многочлен x^8 + x^4 + x^3 + x^2 + 1 --- */
  var EXP = new Array(512), LOG = new Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      EXP[i] = x; LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (var j = 255; j < 512; j++) EXP[j] = EXP[j - 255];
  })();
  function mul(a, b) { return a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]]; }

  function generator(degree) {
    var poly = [1];
    for (var i = 0; i < degree; i++) {
      var next = new Array(poly.length + 1).fill(0);
      for (var j = 0; j < poly.length; j++) {
        next[j] ^= poly[j];
        next[j + 1] ^= mul(poly[j], EXP[i]);
      }
      poly = next;
    }
    return poly;
  }

  function remainder(data, gen) {
    var degree = gen.length - 1, rem = new Array(degree).fill(0);
    for (var i = 0; i < data.length; i++) {
      var factor = data[i] ^ rem[0];
      rem.shift(); rem.push(0);
      for (var j = 0; j < degree; j++) rem[j] ^= mul(gen[j + 1], factor);
    }
    return rem;
  }

  function utf8(text) {
    if (typeof TextEncoder !== "undefined") return Array.prototype.slice.call(new TextEncoder().encode(text));
    var out = [], escaped = unescape(encodeURIComponent(text));
    for (var i = 0; i < escaped.length; i++) out.push(escaped.charCodeAt(i));
    return out;
  }

  function dataCapacity(version) {
    return VERSIONS[version][2].reduce(function (sum, g) { return sum + g[0] * g[1]; }, 0);
  }

  function countBits(version) { return version < 10 ? 8 : 16; }

  function pickVersion(length) {
    for (var v = 1; v <= MAX_VERSION; v++) {
      if (4 + countBits(v) + 8 * length <= dataCapacity(v) * 8) return v;
    }
    throw new Error("текст слишком длинный для QR-кода");
  }

  /* Слова данных: признак режима, длина, сами байты, завершитель и заполнение до ёмкости версии
   * (ISO/IEC 18004, 7.4.9–7.4.10: до четырёх нулевых бит, затем слова 11101100 и 00010001 по очереди). */
  function dataWords(bytes, version) {
    var capacity = dataCapacity(version), bits = [];
    function push(value, width) { for (var i = width - 1; i >= 0; i--) bits.push((value >>> i) & 1); }
    push(4, 4);
    push(bytes.length, countBits(version));
    bytes.forEach(function (b) { push(b, 8); });
    var room = capacity * 8;
    push(0, Math.min(4, room - bits.length));
    while (bits.length % 8) bits.push(0);
    for (var pad = 0xec; bits.length < room; pad ^= 0xec ^ 0x11) push(pad, 8);

    var data = [];
    for (var i = 0; i < bits.length; i += 8) {
      var value = 0;
      for (var j = 0; j < 8; j++) value = (value << 1) | bits[i + j];
      data.push(value);
    }
    return data;
  }

  /* Кодовые слова целиком: данные, разбитые на блоки, со словами коррекции, вперемежку. */
  function codewords(bytes, version) {
    var data = dataWords(bytes, version);
    var spec = VERSIONS[version], gen = generator(spec[1]), blocks = [], offset = 0;
    spec[2].forEach(function (group) {
      for (var n = 0; n < group[0]; n++) {
        var chunk = data.slice(offset, offset + group[1]);
        offset += group[1];
        blocks.push({ data: chunk, ec: remainder(chunk, gen) });
      }
    });
    var out = [], longest = Math.max.apply(null, blocks.map(function (b) { return b.data.length; }));
    for (var k = 0; k < longest; k++) blocks.forEach(function (b) { if (k < b.data.length) out.push(b.data[k]); });
    for (var e = 0; e < spec[1]; e++) blocks.forEach(function (b) { out.push(b.ec[e]); });
    return out;
  }

  var MASKS = [
    function (x, y) { return (x + y) % 2 === 0; },
    function (x, y) { return y % 2 === 0; },
    function (x, y) { return x % 3 === 0; },
    function (x, y) { return (x + y) % 3 === 0; },
    function (x, y) { return (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0; },
    function (x, y) { return (x * y) % 2 + (x * y) % 3 === 0; },
    function (x, y) { return ((x * y) % 2 + (x * y) % 3) % 2 === 0; },
    function (x, y) { return ((x + y) % 2 + (x * y) % 3) % 2 === 0; }
  ];

  function blank(size) {
    var rows = [];
    for (var y = 0; y < size; y++) rows.push(new Array(size).fill(false));
    return rows;
  }

  /* Служебные узоры. Возвращает {modules, reserved}: reserved — куда данные класть нельзя. */
  function frame(version) {
    var size = 17 + 4 * version, modules = blank(size), reserved = blank(size);
    function set(x, y, dark) { modules[y][x] = dark; reserved[y][x] = true; }

    function finder(cx, cy) {
      for (var dy = -4; dy <= 4; dy++) for (var dx = -4; dx <= 4; dx++) {
        var x = cx + dx, y = cy + dy;
        if (x < 0 || y < 0 || x >= size || y >= size) continue;
        var ring = Math.max(Math.abs(dx), Math.abs(dy));
        set(x, y, ring !== 2 && ring !== 4);
      }
    }
    finder(3, 3); finder(size - 4, 3); finder(3, size - 4);

    for (var i = 8; i < size - 8; i++) { set(i, 6, i % 2 === 0); set(6, i, i % 2 === 0); }

    var centers = ALIGN[version], last = centers.length - 1;
    centers.forEach(function (cy, a) {
      centers.forEach(function (cx, b) {
        if ((a === 0 && b === 0) || (a === 0 && b === last) || (a === last && b === 0)) return;
        for (var dy = -2; dy <= 2; dy++) for (var dx = -2; dx <= 2; dx++) {
          set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      });
    });

    /* Место под сведения о формате (заполняется после выбора маски) и тёмный модуль. */
    for (var f = 0; f <= 8; f++) {
      if (!reserved[8][f]) set(f, 8, false);
      if (!reserved[f][8]) set(8, f, false);
    }
    for (var g = 0; g < 8; g++) { set(size - 1 - g, 8, false); set(8, size - 1 - g, false); }
    set(8, size - 8, true);

    if (version >= 7) {
      var rem = version;
      for (var r = 0; r < 12; r++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      var info = (version << 12) | rem;
      for (var k = 0; k < 18; k++) {
        var dark = ((info >>> k) & 1) === 1, a = size - 11 + (k % 3), b = Math.floor(k / 3);
        set(a, b, dark); set(b, a, dark);
      }
    }
    return { size: size, modules: modules, reserved: reserved };
  }

  function placeData(grid, words) {
    var size = grid.size, total = words.length * 8, index = 0, upward = true;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (var step = 0; step < size; step++) {
        var y = upward ? size - 1 - step : step;
        for (var j = 0; j < 2; j++) {
          var x = right - j;
          if (grid.reserved[y][x]) continue;
          var dark = false;
          if (index < total) dark = ((words[index >>> 3] >>> (7 - (index & 7))) & 1) === 1;
          index++;
          grid.modules[y][x] = dark;
        }
      }
      upward = !upward;
    }
  }

  function withMask(grid, mask) {
    var size = grid.size, out = blank(size), fn = MASKS[mask];
    for (var y = 0; y < size; y++) for (var x = 0; x < size; x++) {
      out[y][x] = grid.reserved[y][x] ? grid.modules[y][x] : grid.modules[y][x] !== fn(x, y);
    }
    /* Сведения о формате: уровень M (биты 00) и номер маски, с защитой BCH(15,5). */
    var data = mask, rem = data;
    for (var i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    var bits = ((data << 10) | rem) ^ 0x5412;
    function bit(n) { return ((bits >>> n) & 1) === 1; }
    for (var a = 0; a <= 5; a++) out[a][8] = bit(a);
    out[7][8] = bit(6); out[8][8] = bit(7); out[8][7] = bit(8);
    for (var b = 9; b < 15; b++) out[8][14 - b] = bit(b);
    for (var c = 0; c < 8; c++) out[8][size - 1 - c] = bit(c);
    for (var d = 8; d < 15; d++) out[size - 15 + d][8] = bit(d);
    out[size - 8][8] = true;
    return out;
  }

  /* Штраф маски: чем меньше, тем легче коду читаться. Четыре правила стандарта. */
  function penalty(rows) {
    var size = rows.length, score = 0, x, y, run, dark = 0;
    function line(get) {
      var points = 0, length = 1;
      for (var i = 1; i < size; i++) {
        if (get(i) === get(i - 1)) { length++; continue; }
        if (length >= 5) points += length - 2;
        length = 1;
      }
      if (length >= 5) points += length - 2;
      return points;
    }
    function finderLike(get) {
      var points = 0, a = [true, false, true, true, true, false, true, false, false, false, false];
      for (var i = 0; i + 11 <= size; i++) {
        var forward = true, backward = true;
        for (var k = 0; k < 11; k++) {
          var value = get(i + k);
          if (value !== a[k]) forward = false;
          if (value !== a[10 - k]) backward = false;
        }
        if (forward) points += 40;
        if (backward) points += 40;
      }
      return points;
    }
    for (y = 0; y < size; y++) {
      run = (function (row) { return function (i) { return rows[row][i]; }; })(y);
      score += line(run) + finderLike(run);
    }
    for (x = 0; x < size; x++) {
      run = (function (col) { return function (i) { return rows[i][col]; }; })(x);
      score += line(run) + finderLike(run);
    }
    for (y = 0; y < size - 1; y++) for (x = 0; x < size - 1; x++) {
      var v = rows[y][x];
      if (v === rows[y][x + 1] && v === rows[y + 1][x] && v === rows[y + 1][x + 1]) score += 3;
    }
    for (y = 0; y < size; y++) for (x = 0; x < size; x++) if (rows[y][x]) dark++;
    score += Math.floor(Math.abs(dark * 20 - size * size * 10) / (size * size)) * 10;
    return score;
  }

  /* Матрица QR-кода: {size, version, mask, rows} — rows[y][x] истинно для тёмного модуля.
   * options.version и options.mask задают их жёстко (для проверки). */
  function matrix(text, options) {
    options = options || {};
    var bytes = utf8(String(text));
    var version = options.version || pickVersion(bytes.length);
    if (version < 1 || version > MAX_VERSION || 4 + countBits(version) + 8 * bytes.length > dataCapacity(version) * 8) {
      throw new Error("текст не помещается в QR-код этой версии");
    }
    var grid = frame(version);
    placeData(grid, codewords(bytes, version));
    var mask = options.mask, rows;
    if (mask === undefined || mask === null) {
      var best = Infinity;
      for (var m = 0; m < 8; m++) {
        var candidate = withMask(grid, m), points = penalty(candidate);
        if (points < best) { best = points; mask = m; rows = candidate; }
      }
    } else {
      rows = withMask(grid, mask);
    }
    return { size: grid.size, version: version, mask: mask, rows: rows };
  }

  /* SVG-элемент с кодом. Всегда тёмное на белом, с полями: иначе камера его не прочтёт.
   * Строится через DOM, без встроенных стилей — страница работает под строгой политикой содержимого. */
  function svg(text, options) {
    options = options || {};
    var code = matrix(text), quiet = options.quiet === undefined ? 4 : options.quiet;
    var full = code.size + quiet * 2, ns = "http://www.w3.org/2000/svg", path = "";
    for (var y = 0; y < code.size; y++) {
      for (var x = 0; x < code.size; x++) {
        if (!code.rows[y][x]) continue;
        var start = x;
        while (x + 1 < code.size && code.rows[y][x + 1]) x++;
        path += "M" + (start + quiet) + " " + (y + quiet) + "h" + (x - start + 1) + "v1h-" + (x - start + 1) + "z";
      }
    }
    var el = document.createElementNS(ns, "svg");
    el.setAttribute("viewBox", "0 0 " + full + " " + full);
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", options.label || "QR-код");
    el.setAttribute("shape-rendering", "crispEdges");
    var back = document.createElementNS(ns, "rect");
    back.setAttribute("width", full); back.setAttribute("height", full); back.setAttribute("fill", "#ffffff");
    var dots = document.createElementNS(ns, "path");
    dots.setAttribute("d", path); dots.setAttribute("fill", "#000000");
    el.appendChild(back); el.appendChild(dots);
    return el;
  }

  var api = {
    matrix: matrix, svg: svg, MAX_BYTES: 213,
    /* Только для проверки: слова данных до коррекции ошибок. */
    dataWords: function (text, version) { return dataWords(utf8(String(text)), version); }
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ShturmanQR = api;
})(typeof window !== "undefined" ? window : this);
