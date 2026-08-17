/* Minimal QR code encoder (byte mode, error-correction level L, versions 1-6).
 * No dependencies -- versions 1-6 top out at 134 bytes, comfortably more than
 * "http://192.168.x.x:8080", and staying in that range avoids the mixed
 * block-size layouts that show up at higher versions/levels.
 */
(function (global) {
  "use strict";

  const GF_EXP = new Array(512);
  const GF_LOG = new Array(256);
  (function () {
    let x = 1;
    for (let i = 0; i < 255; i++) {
      GF_EXP[i] = x;
      GF_LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (let i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
  })();

  function gfMul(a, b) {
    if (a === 0 || b === 0) return 0;
    return GF_EXP[GF_LOG[a] + GF_LOG[b]];
  }

  function multiplyPoly(a, b) {
    const out = new Array(a.length + b.length - 1).fill(0);
    for (let i = 0; i < a.length; i++) {
      for (let j = 0; j < b.length; j++) out[i + j] ^= gfMul(a[i], b[j]);
    }
    return out;
  }

  function generatorPoly(degree) {
    let g = [1];
    for (let i = 0; i < degree; i++) g = multiplyPoly(g, [1, GF_EXP[i]]);
    return g;
  }

  function rsEncode(dataBytes, ecCount) {
    const gen = generatorPoly(ecCount);
    const msg = dataBytes.concat(new Array(ecCount).fill(0));
    for (let i = 0; i < dataBytes.length; i++) {
      const coef = msg[i];
      if (coef !== 0) {
        for (let j = 0; j < gen.length; j++) msg[i + j] ^= gfMul(gen[j], coef);
      }
    }
    return msg.slice(dataBytes.length);
  }

  // Total data codewords, EC codewords/block, and block count -- level L, v1-6.
  const DATA_CW = { 1: 19, 2: 34, 3: 55, 4: 80, 5: 108, 6: 136 };
  const EC_PER_BLOCK = { 1: 7, 2: 10, 3: 15, 4: 20, 5: 26, 6: 18 };
  const BLOCK_COUNT = { 1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 2 };
  const ALIGN_POS = { 2: 18, 3: 22, 4: 26, 5: 30, 6: 34 };

  function pickVersion(byteLen) {
    for (let v = 1; v <= 6; v++) {
      const bits = 4 + 8 + byteLen * 8;
      if (bits <= DATA_CW[v] * 8) return v;
    }
    return null;
  }

  function buildCodewords(text, version) {
    const bytes = Array.from(new TextEncoder().encode(text));
    const capacityBits = DATA_CW[version] * 8;
    const bits = [];
    const pushBits = (value, len) => {
      for (let i = len - 1; i >= 0; i--) bits.push((value >> i) & 1);
    };
    pushBits(0b0100, 4);
    pushBits(bytes.length, 8);
    for (const b of bytes) pushBits(b, 8);
    for (let i = 0; i < 4 && bits.length < capacityBits; i++) bits.push(0);
    while (bits.length % 8 !== 0) bits.push(0);
    const padBytes = [0xec, 0x11];
    let p = 0;
    while (bits.length < capacityBits) {
      pushBits(padBytes[p % 2], 8);
      p++;
    }
    const codewords = [];
    for (let i = 0; i < bits.length; i += 8) {
      let byte = 0;
      for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
      codewords.push(byte);
    }
    return codewords;
  }

  function interleave(codewords, version) {
    const blocks = BLOCK_COUNT[version];
    const ec = EC_PER_BLOCK[version];
    const perBlock = codewords.length / blocks;
    const dataBlocks = [];
    const ecBlocks = [];
    for (let i = 0; i < blocks; i++) {
      const chunk = codewords.slice(i * perBlock, (i + 1) * perBlock);
      dataBlocks.push(chunk);
      ecBlocks.push(rsEncode(chunk, ec));
    }
    const out = [];
    for (let i = 0; i < perBlock; i++) {
      for (const block of dataBlocks) out.push(block[i]);
    }
    for (let i = 0; i < ec; i++) {
      for (const block of ecBlocks) out.push(block[i]);
    }
    return out;
  }

  function bchFormatBits(data5) {
    const G15 = 0x537;
    const G15_MASK = 0x5412;
    let d = data5 << 10;
    let shifted = G15;
    let dLen = 32 - Math.clz32(d);
    let gLen = 32 - Math.clz32(G15);
    while (dLen >= gLen) {
      d ^= G15 << (dLen - gLen);
      dLen = d === 0 ? 0 : 32 - Math.clz32(d);
    }
    return ((data5 << 10) | d) ^ G15_MASK;
  }

  function generate(text) {
    const version = pickVersion(new TextEncoder().encode(text).length);
    if (!version) throw new Error("text too long for this QR encoder");
    const codewords = buildCodewords(text, version);
    const finalCodewords = interleave(codewords, version);
    const size = 17 + 4 * version;

    const grid = Array.from({ length: size }, () => new Array(size).fill(null));
    const setModule = (r, c, dark, isFunction) => {
      if (r < 0 || r >= size || c < 0 || c >= size) return;
      grid[r][c] = { dark, isFunction };
    };

    const finder = (row, col) => {
      for (let r = -1; r <= 7; r++) {
        if (row + r <= -1 || row + r >= size) continue;
        for (let c = -1; c <= 7; c++) {
          if (col + c <= -1 || col + c >= size) continue;
          const dark =
            (r >= 0 && r <= 6 && (c === 0 || c === 6)) ||
            (c >= 0 && c <= 6 && (r === 0 || r === 6)) ||
            (r >= 2 && r <= 4 && c >= 2 && c <= 4);
          setModule(row + r, col + c, dark, true);
        }
      }
    };
    finder(0, 0);
    finder(0, size - 7);
    finder(size - 7, 0);

    for (let i = 8; i < size - 8; i++) {
      if (!grid[6][i]) setModule(6, i, i % 2 === 0, true);
      if (!grid[i][6]) setModule(i, 6, i % 2 === 0, true);
    }

    const pos = ALIGN_POS[version];
    if (pos) {
      for (let r = -2; r <= 2; r++) {
        for (let c = -2; c <= 2; c++) {
          const dark = r === -2 || r === 2 || c === -2 || c === 2 || (r === 0 && c === 0);
          setModule(pos + r, pos + c, dark, true);
        }
      }
    }

    const setupTypeInfo = (bits) => {
      const bit = (i) => ((bits >> i) & 1) === 1;
      for (let c = 0; c <= 5; c++) setModule(8, c, bit(c), true);
      setModule(8, 7, bit(6), true);
      setModule(8, 8, bit(7), true);
      setModule(7, 8, bit(8), true);
      for (let r = 5; r >= 0; r--) setModule(r, 8, bit(9 + (5 - r)), true);
      for (let k = 0; k <= 7; k++) setModule(8, size - 1 - k, bit(k), true);
      for (let k = 0; k <= 6; k++) setModule(size - 7 + k, 8, bit(8 + k), true);
      setModule(size - 8, 8, true, true);
    };
    setupTypeInfo(0);

    const bitLen = finalCodewords.length * 8;
    const getBit = (i) => {
      if (i >= bitLen) return 0;
      const byte = finalCodewords[i >> 3];
      return (byte >> (7 - (i % 8))) & 1;
    };
    let bitIndex = 0;
    let dir = -1;
    let row = size - 1;
    for (let col = size - 1; col > 0; col -= 2) {
      if (col === 6) col--;
      for (;;) {
        for (let c = 0; c < 2; c++) {
          const cc = col - c;
          if (!grid[row][cc]) {
            grid[row][cc] = { dark: getBit(bitIndex) === 1, isFunction: false };
            bitIndex++;
          }
        }
        row += dir;
        if (row < 0 || row >= size) {
          row -= dir;
          dir = -dir;
          break;
        }
      }
    }

    for (let r = 0; r < size; r++) {
      for (let c = 0; c < size; c++) {
        const cell = grid[r][c];
        if (cell && !cell.isFunction && (r + c) % 2 === 0) cell.dark = !cell.dark;
      }
    }

    const eccIndicator = 0b01; // L
    const maskPattern = 0;
    setupTypeInfo(bchFormatBits((eccIndicator << 3) | maskPattern));

    return {
      size,
      isDark: (r, c) => !!(grid[r][c] && grid[r][c].dark),
    };
  }

  function renderCanvas(canvas, text, options) {
    const opts = options || {};
    const scale = opts.scale || 6;
    const margin = opts.margin != null ? opts.margin : 4;
    const qr = generate(text);
    const px = (qr.size + margin * 2) * scale;
    canvas.width = px;
    canvas.height = px;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = opts.light || "#ffffff";
    ctx.fillRect(0, 0, px, px);
    ctx.fillStyle = opts.dark || "#000000";
    for (let r = 0; r < qr.size; r++) {
      for (let c = 0; c < qr.size; c++) {
        if (qr.isDark(r, c)) {
          ctx.fillRect((c + margin) * scale, (r + margin) * scale, scale, scale);
        }
      }
    }
    return qr;
  }

  global.QRCode = { generate, renderCanvas };
})(window);
