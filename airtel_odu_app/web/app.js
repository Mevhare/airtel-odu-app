'use strict';

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
const LIVE_MS = 1000;
let loggedIn = false;
const LIVE_POINTS = 180;    // one a second, so the window is three minutes wide

const live = { down: [], up: [], ts: [], hover: null };
// Restore whatever of the last three minutes is still fresh, so the chart
// isn't blank every time the page is opened or refreshed. This is an instant
// paint from this browser's own cache; it's superseded moments later by the
// server's copy below, which is what makes a brand new tab or phone start
// populated too.
try {
  const saved = JSON.parse(localStorage.getItem('wifiapp-live') || 'null');
  if (saved && Array.isArray(saved.ts)) {
    const cutoff = Date.now() / 1000 - LIVE_POINTS;
    saved.ts.forEach((ts, i) => {
      if (ts < cutoff) return;
      live.ts.push(ts); live.down.push(saved.down[i]); live.up.push(saved.up[i]);
    });
  }
} catch { /* corrupt or unavailable cache -- start empty */ }

let writesEnabled = false;
let currentMode = null;
let modes = {};
let autoReset = null;
let lastSignal = { strength: null, clarity: null };  // for the optimisation modes
let optimiseMode = 'default';

const usage = {
  range: 'week',       // preset name, or 'custom'
  group: '',            // '' = let the server pick the bucket, else an explicit override
  since: null,          // custom range only, unix seconds
  until: null,          // custom range only, unix seconds
  points: [],
  bucket: 'day',
  selected: null,      // index the user tapped
  hover: null,         // index the pointer is over
};

let cycleInfo = null;  // start/end of the current billing cycle, for labels

let deviceItems = [];
let deviceStamp = null;   // when the router last answered
let deviceClientIp = null;   // this browser's own LAN IP, to badge its row
let deviceTrackedSince = null;   // when our own per-device usage ledger starts
const deviceRange = {
  range: 'week',       // preset name, or 'custom'
  since: null,          // custom range only, unix seconds
  until: null,          // custom range only, unix seconds
};
let lastErrors = {};      // which of the two boxes is currently unreachable

// -- formatting -------------------------------------------------------------

function bytes(count) {
  if (count === null || count === undefined) return '—';
  let size = Number(count);
  if (!isFinite(size)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
  return size.toFixed(size >= 100 || unit === 0 ? 0 : 1) + ' ' + units[unit];
}

function rate(perSecond) {
  const bits = Number(perSecond || 0) * 8;
  if (bits >= 1e9) return (bits / 1e9).toFixed(2) + ' Gbps';
  if (bits >= 1e6) return (bits / 1e6).toFixed(1) + ' Mbps';
  if (bits >= 1e3) return (bits / 1e3).toFixed(0) + ' kbps';
  return '0 Mbps';
}

// "<span class=good>▼ 6%</span> <span class=delta-label>vs yesterday</span>" --
// blank whenever there is nothing sound to compare against (no prior period,
// or it used zero bytes, which would make the percentage meaningless or
// infinite). Only the figure itself is coloured -- more data used than before
// is red, less is green, for a usage metric where up is bad -- the "vs ..."
// text stays neutral so it doesn't read as good or bad on its own.
function pctChange(current, previous, label) {
  if (current === null || current === undefined
    || previous === null || previous === undefined || previous <= 0) return '';
  const change = ((current - previous) / previous) * 100;
  const figure = change === 0
    ? '<span class="delta">—</span>'
    : `<span class="delta ${change > 0 ? 'bad' : 'good'}">${change > 0 ? '▲' : '▼'} `
      + `${Math.abs(change).toFixed(0)}%</span>`;
  return label ? `${figure} <span class="delta-label">vs ${label}</span>` : figure;
}

function duration(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

const clock = (ts) => new Date(ts * 1000)
  .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

const day = (ts) => new Date(ts * 1000)
  .toLocaleDateString([], { day: 'numeric', month: 'short' });

const dated = (ts) => new Date(ts * 1000)
  .toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });

// The devices quote dates as 2025/11/01; nobody says that out loud.
function prettyDate(value) {
  const parts = String(value || '').split(/[/-]/).map(Number);
  if (parts.length !== 3 || parts.some((n) => !isFinite(n))) return value || '';
  return new Date(parts[0], parts[1] - 1, parts[2])
    .toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });
}

const num = (value, digits = 0) =>
  (value === null || value === undefined || value === '')
    ? '—' : Number(value).toFixed(digits);

function formatNumber(msisdn) {
  const digits = String(msisdn || '').replace(/\D/g, '');
  const local = digits.length === 10 ? '0' + digits
    : digits.startsWith('234') ? '0' + digits.slice(3) : digits;
  return local.length === 11
    ? `${local.slice(0, 4)} ${local.slice(4, 7)} ${local.slice(7)}` : local;
}

function escapeHtml(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

// The firmware's own vocabulary, translated into what people actually say.
const TECH = {
  ENDC: '5G (NSA)', NR5G: '5G', NR: '5G', LTE: '4G', 'LTE-A': '4G+',
  WCDMA: '3G', HSPA: '3G', GSM: '2G', NO_SERVICE: 'No service',
  Limited_Service: 'Limited service',
};

const GRADE_TEXT = {
  excellent: 'Excellent signal', good: 'Good signal',
  fair: 'Fair signal', poor: 'Weak signal',
};

// severity, headline, and what it actually means — the third one is folded away
// until the row is tapped.
const EVENTS = {
  link_up: ['good', 'Internet came back',
    'The outdoor unit reported a working mobile connection again. Anything that '
    + 'was downloading when it dropped had to restart, but nothing is lost. A '
    + 'drop and a return within a few seconds of each other is just the link '
    + 'blinking, and is normal now and then.'],
  link_down: ['bad', 'Internet dropped',
    'The outdoor unit lost its mobile connection. Your WiFi kept running — '
    + 'devices stayed connected to the router — but nothing could reach the '
    + 'internet through it. Usual causes are the mast dropping the session, heavy '
    + 'rain weakening the signal, or the unit restarting. If this keeps happening '
    + 'at the same time each day, the mast is probably congested then.'],
  data_cap: ['warn', 'Data allowance milestone',
    'Usage measured by this dashboard crossed one of the marks set in config '
    + '(half, four fifths, and all of the allowance). This is our own count from '
    + 'the outdoor unit\'s byte counters, not Airtel\'s billing.'],
  weak_signal: ['warn', 'Signal fell to a weak level',
    'Signal strength stayed under the warning level for five minutes without '
    + 'recovering, so it was not a momentary dip. Expect slower speeds while it '
    + 'lasts. If it does not come back on its own, the outdoor unit may have '
    + 'shifted, or the mast it was using has changed.'],
  mode_change: ['', 'Network mode changed',
    'The network mode was changed from the Settings tab — for example locking to '
    + '4G, or allowing 5G. The connection drops for ten to thirty seconds when '
    + 'this happens, which is why a drop and a return often follow this entry.'],
  mode_revert: ['warn', 'Network mode restored automatically',
    'The mode that was selected did not produce a working connection inside the '
    + 'safety window, so the previous one was put back without being asked. '
    + 'Nothing needs doing — the connection you had before is what you have now.'],
  mode_revert_failed: ['bad', 'Could not restore the previous mode',
    'The safety net tried to put the previous network mode back and the outdoor '
    + 'unit refused. The connection may still be down. Open Network mode in '
    + 'Settings and set it back by hand, or restart the outdoor unit.'],
  reboot: ['', 'Restart requested',
    'A restart was asked for from this dashboard. The device takes a few minutes '
    + 'to come back, and everything is offline meanwhile.'],
  auto_reset: ['', 'Monthly counter reset changed',
    'The outdoor unit\'s own monthly counter reset was switched on or off from '
    + 'this dashboard. It changes what the unit reports as its "month" figure; it '
    + 'does not affect the count this dashboard keeps.'],
  billing_day: ['', 'Billing cycle start date changed',
    'The day of the month this dashboard resets its own usage count was changed '
    + 'from Settings. It only affects this dashboard\'s tracking, not the outdoor '
    + 'unit\'s own counters.'],
  sms: ['', 'Text message arrived',
    'A text arrived on the SIM that was not one of Airtel\'s routine daily usage '
    + 'figures — so it is worth reading. The full message is on the Texts tab.'],
};

// -- views ------------------------------------------------------------------

function show(name) {
  document.querySelectorAll('.view').forEach((view) => {
    view.hidden = view.id !== 'view-' + name;
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === name);
  });
  localStorage.setItem('wifiapp-view', name);
  window.scrollTo(0, 0);
  // The canvases read their own pixel size on every draw, and that size is 0
  // while their view is hidden. Coming back needs an explicit redraw rather
  // than waiting for the next scheduled tick to happen to notice.
  if (name === 'home') drawLive();
  if (name === 'usage') { loadUsage(); drawUsage(); }
  if (name === 'sms') loadSms();
  if (name === 'settings') { loadNetworkConfig(); loadPhoneQr(); }
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.view));
});
document.querySelectorAll('[data-goto]').forEach((button) => {
  button.addEventListener('click', () => {
    show(button.dataset.goto);
    const target = button.dataset.scrollTo && $(button.dataset.scrollTo);
    if (target instanceof HTMLDetailsElement) target.open = true;
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
});

// -- overview ---------------------------------------------------------------

function renderOverview(data) {
  writesEnabled = data.writes_enabled;
  modes = data.modes || {};

  lastErrors = data.errors || {};
  const errors = Object.entries(lastErrors);
  const banner = $('banner');
  banner.hidden = !errors.length;
  if (errors.length) banner.textContent = errors.map(([k, v]) => `${k}: ${v}`).join('  •  ');

  /* The two boxes fail independently: the router can be answering while the
     outdoor unit is rebooting, and vice versa. Neither case may blank the page,
     so whatever is still readable stays on screen and the rest says why. */
  const odu = data.odu;
  if (!odu) {
    $('dot').className = 'dot down';
    setBumped($('status'), 'Outdoor unit unreachable');
    $('status-sub').textContent = data.errors && data.errors.odu
      ? 'Nothing has been read from it yet this session. The WiFi itself may be '
        + 'fine — check the Devices tab.'
      : 'Waiting for the first reading…';
    return;
  }

  const age = Math.round(Date.now() / 1000 - odu.ts);
  const stale = age > 30;

  const info = odu.netinfo || {};
  const primary = (odu.carriers && odu.carriers[0]) || {};

  $('dot').className = 'dot ' + (stale ? '' : odu.connected ? 'up' : 'down');
  const carrier = info.network_provider_fullname || info.network_provider || '—';
  $('operator').textContent = carrier;

  // The SIM does not carry an MSISDN, so the number comes from Airtel's own
  // texts, which quote it. Nothing shows until one has been read.
  const number = $('hero-number');
  number.hidden = !odu.msisdn;
  if (odu.msisdn) number.textContent = formatNumber(odu.msisdn);
  setBumped($('status'), stale ? 'Out of touch'
    : odu.connected ? 'Online' : 'Offline');
  $('status-sub').textContent = stale
    ? `The outdoor unit stopped answering ${duration(age) === '0m' ? 'moments' : duration(age)}`
      + ` ago. Everything below is what it last said, at ${clock(odu.ts)}.`
    : odu.connected
      ? `Connected for ${duration(odu.session_time)} · updated ${clock(odu.ts)}`
      : 'The outdoor unit has no mobile connection right now.';

  const tech = TECH[info.network_type] || info.network_type || '—';
  $('pill-tech').textContent = tech + (info.wan_active_band ? ' · ' + info.wan_active_band : '');
  $('pill-tech').className = 'pill tech';
  renderGrade(odu.grade);
  renderJitter(primary.sinr ?? info.lte_snr);

  const devices = (data.devices && data.devices.items) || [];
  $('devices-text').textContent = devices.length + (devices.length === 1 ? ' device' : ' devices');

  if (data.optimise_mode) markOptimise(data.optimise_mode);
  renderSignal(primary.rsrp ?? info.lte_rsrp, primary.sinr ?? info.lte_snr);

  $('uptime').textContent = data.uptime_24h === null || data.uptime_24h === undefined
    ? '—' : (data.uptime_24h * 100).toFixed(1) + '%';

  renderCap(data.projection, data.cap);
  renderModes(info.net_select);
  renderRadio(info, odu.carriers || []);
  renderAutoReset(odu.settings || {});

  const billingInput = $('billing-day-input');
  if (document.activeElement !== billingInput) billingInput.value = data.billing_day || 1;
}

/* Only worth animating when the words actually change — a value that ticks over
   every second and jumps about is noise, not feedback. */
function setBumped(node, text) {
  if (node.textContent === text) return;
  node.textContent = text;
  node.classList.remove('bump');
  void node.offsetWidth;          // restart the animation
  node.classList.add('bump');
}

// Four bars, the way a phone draws them, from the grade the server worked out.
const GRADE_BARS = { excellent: 4, good: 3, fair: 2, poor: 1 };

function renderGrade(grade) {
  const filled = GRADE_BARS[grade] || 0;
  $('bars').innerHTML = [1, 2, 3, 4]
    .map((n) => `<i class="${n <= filled ? 'on' : ''}"></i>`).join('');
  setBumped($('grade-text'), GRADE_TEXT[grade] || '—');
  $('pill-grade').className = 'pill ' + (grade || '');
}

// SINR tracks connection steadiness (jitter, spikes) far better than RSRP —
// signal can look strong while noise still wrecks the connection.
const JITTER_TEXT = { good: 'Good for gaming', fair: 'OK for gaming', poor: 'Bad for gaming' };

function renderJitter(sinr) {
  let grade = null;
  if (sinr !== null && sinr !== undefined) {
    grade = sinr >= 10 ? 'good' : sinr >= 3 ? 'fair' : 'poor';
  }
  $('pill-jitter').className = 'pill pill-icon ' + (grade || '');
  setBumped($('jitter-text'), JITTER_TEXT[grade] || '—');
}

/* Two numbers that sound alike and mean very different things, so both get a
   word underneath and a paragraph that says what the pair means together. */
const RSRP_WORDS = [
  [-80, 'very strong'], [-90, 'strong'], [-100, 'usable'],
  [-110, 'weak'], [-999, 'very weak'],
];
const SINR_WORDS = [
  [20, 'very clear'], [13, 'clear'], [6, 'a bit noisy'],
  [0, 'noisy'], [-999, 'lost in noise'],
];

const wordFor = (table, value) =>
  (table.find(([threshold]) => value >= threshold) || table[table.length - 1])[1];

function renderSignal(rsrp, sinr) {
  const strength = rsrp === null || rsrp === undefined ? null : Number(rsrp);
  const clarity = sinr === null || sinr === undefined ? null : Number(sinr);
  lastSignal = { strength, clarity };

  const rows = [
    ['Signal', strength === null ? null
      : `${num(strength)} dBm — ${wordFor(RSRP_WORDS, strength)}`],
    ['Clarity', clarity === null ? null
      : `${num(clarity, 1)} dB — ${wordFor(SINR_WORDS, clarity)}`],
  ].filter(([, value]) => value !== null);
  $('signal-rows').innerHTML = rows.length
    ? rows.map(([key, value]) =>
        `<div><span class="key">${key}</span><span class="val">${value}</span></div>`).join('')
    : '<div><span class="key">No signal reading yet.</span></div>';

  if (strength === null || clarity === null) {
    $('signal-explain').textContent = '';
    return;
  }

  const loud = strength >= -95;
  const clean = clarity >= 13;
  let verdict;
  if (loud && clean) {
    verdict = 'Both are healthy — this link is doing as well as this location allows.';
  } else if (loud && !clean) {
    verdict = 'Strong signal, but crowded airwaves are holding speeds back. Usually '
      + 'clears up outside peak hours.';
  } else if (!loud && clean) {
    verdict = 'Clean but faint. Aiming the outdoor unit more precisely at the mast '
      + 'would help.';
  } else {
    verdict = 'Faint and noisy — the combination that causes stalling. Worth '
      + 'checking the outdoor unit hasn\'t shifted.';
  }

  $('signal-explain').textContent =
    `Signal: ${num(strength)} dBm, ${wordFor(RSRP_WORDS, strength)} (closer to `
    + `zero is better). Clarity: ${num(clarity, 1)} dB, ${wordFor(SINR_WORDS, clarity)} `
    + `(above 13 is clean, below 5 hurts). ${verdict}`;
}

function renderAutoReset(settings) {
  autoReset = settings.auto_reset || null;
  const note = $('reset-note');
  const button = $('toggle-reset');

  if (!autoReset) {
    note.textContent = 'Could not read the outdoor unit’s counter settings.';
    button.disabled = true;
    button.textContent = '—';
    return;
  }

  const cleared = settings.counters_cleared;
  button.disabled = !writesEnabled;
  button.textContent = autoReset.enabled
    ? 'Turn monthly reset off' : 'Turn monthly reset on';

  note.textContent = autoReset.enabled
    ? `The outdoor unit resets its own counters on day ${autoReset.day} each month.`
    : `The outdoor unit's own counter has been running since `
      + `${prettyDate(cleared) || 'setup'} with no monthly reset — turn this on `
      + 'for it to clear itself automatically.';
}

// "This cycle" on its own means nothing, so every mention of it names the dates.
const cycleWindow = (cycle) =>
  `${day(cycle.start_ts)} – ${day(cycle.end_ts - 86400)}`;

function renderCap(cycle, cap) {
  cycleInfo = cycle || null;
  if (!cycle) {
    $('cap-block').hidden = true;
    $('cycle-range').textContent = '';
    $('usage-headline').textContent = '—';
    $('cycle-total').textContent = '—';
    $('cycle-total-delta').innerHTML = '';
    $('cycle-today').textContent = '—';
    $('cycle-today-delta').innerHTML = '';
    $('cycle-avg').textContent = '—';
    $('cycle-avg-delta').innerHTML = '';
    $('cycle-projected').textContent = '—';
    $('cycle-projected-delta').innerHTML = '';
    return;
  }

  // Airtel's own daily figures, not the ODU's own running counters. Today
  // stays compared against yesterday; the "vs last cycle" comparison only
  // appears once, on the cycle total -- average and projected are scalar
  // multiples of the same total, so their own "vs last cycle" figures would
  // just repeat this one.
  const isCurrent = cycle.today_ts && isToday(cycle.today_ts);
  $('cycle-total').textContent = bytes(cycle.used);
  $('cycle-total-delta').innerHTML = pctChange(cycle.used, cycle.prev_used, 'last cycle');
  $('cycle-today-label').textContent = isCurrent ? 'Today' : cycle.today_ts ? day(cycle.today_ts) : 'Today';
  $('cycle-today').textContent = cycle.today === null ? '—' : bytes(cycle.today);
  $('cycle-today-delta').innerHTML = pctChange(cycle.today, cycle.yesterday,
    isCurrent || !cycle.yesterday_ts ? 'yesterday' : day(cycle.yesterday_ts));
  $('cycle-avg').textContent = cycle.avg_per_day === null ? '—' : bytes(cycle.avg_per_day);
  $('cycle-avg-delta').innerHTML = '';
  $('cycle-projected').textContent = cycle.projected_total === null
    ? '—' : bytes(cycle.projected_total);
  $('cycle-projected-delta').innerHTML = '';

  $('cycle-range').textContent =
    `${cycleWindow(cycle)} · day ${daysBetween(cycle.start_ts, Date.now() / 1000) + 1} of `
    + `${daysBetween(cycle.start_ts, cycle.end_ts)}`;

  // The progress bar only means something once there's a cap to measure against
  // and the cycle's data is known-complete — otherwise it'd show a misleading
  // or pointless percentage, so it stays hidden and the headline/caveat below
  // carry the same information in words instead.
  const showCap = Boolean(cap) && cycle.complete;
  $('cap-block').hidden = !showCap;
  if (showCap) {
    const ratio = Math.min(1, cycle.used / cap);
    $('cap-fill').style.width = (ratio * 100).toFixed(1) + '%';
    $('cap-fill').className = 'fill' + (ratio >= 1 ? ' bad' : ratio >= 0.8 ? ' warn' : '');
    let text = `${bytes(cycle.used)} of ${bytes(cap)} used (${(ratio * 100).toFixed(1)}%) `
      + `since ${day(cycle.start_ts)}.`;
    if (cycle.per_day) {
      text += ` About ${bytes(cycle.per_day)} a day`;
      text += cycle.days_left === null ? '.'
        : ` — at that rate, the allowance runs out around `
          + `${day(Date.now() / 1000 + cycle.days_left * 86400)}.`;
    }
    $('cap-note').textContent = text;
  }

  $('usage-headline').textContent = !cap
    ? `${bytes(cycle.used)} used — part of the ${cycleWindow(cycle)} cycle.`
    : cycle.complete
      ? `${bytes(cycle.used)} of a ${bytes(cap)} allowance, ${cycleWindow(cycle)}.`
      : `${bytes(cycle.used)} so far — part of the ${cycleWindow(cycle)} cycle.`;

  const caveat = $('cycle-caveat');
  caveat.hidden = cycle.complete;
  if (!cycle.complete) {
    caveat.textContent = cycle.source === 'carrier'
      ? `Airtel's texts only go back to ${day(cycle.covered_from)} so far.`
      : 'Waiting on Airtel\'s first usage text for this cycle.';
  }
}

const daysBetween = (from, to) => Math.max(0, Math.floor((to - from) / 86400));
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

function renderModes(current) {
  currentMode = current;
  const container = $('modes');
  container.innerHTML = Object.entries(modes).map(([value, label]) => {
    const [name, desc] = splitLabel(label);
    return `<button class="mode${value === current ? ' active' : ''}" data-mode="${value}"
              ${writesEnabled ? '' : 'disabled'}>
        <span><span class="name">${escapeHtml(name)}</span>
        <span class="desc">${escapeHtml(desc)}</span></span>
        <span class="check">${value === current ? 'In use' : ''}</span>
      </button>`;
  }).join('');

  container.querySelectorAll('.mode').forEach((button) => {
    button.addEventListener('click', () => switchMode(button.dataset.mode));
  });

  $('mode-current').textContent = splitLabel(modes[current] || current || '—')[0];
  $('mode-note').textContent = writesEnabled
    ? 'Drops the connection for 10–30 seconds; reverts automatically if it '
      + 'doesn\'t come back.'
    : 'Locked. Set safety.allow_writes to true in config.json to change the mode.';
}

function splitLabel(label) {
  const match = /^(.*?)\s*\((.*)\)$/.exec(label);
  return match ? [match[1], match[2]] : [label, ''];
}

function renderRadio(info, carriers) {
  $('carrier-rows').innerHTML = carriers.length
    ? carriers.map((carrier) => `
      <div>
        <span class="key">${carrier.primary ? 'Main' : 'Extra ' + carrier.index}
          · band ${num(carrier.band)}</span>
        <span class="val">${num(carrier.bandwidth)} MHz
          <span class="unit">&nbsp;${num(carrier.rsrp)} dBm</span></span>
      </div>`).join('')
    : '<div><span class="key">Only one frequency block in use.</span></div>';

  const rows = [
    ['Cell', info.cell_id],
    ['Physical cell ID', info.lte_pci],
    ['4G bands available', info.lte_band],
    ['5G band in use', info.nr5g_action_band],
    ['5G channel width', info.nr5g_bandwidth ? info.nr5g_bandwidth + ' MHz' : ''],
    ['5G signal', info.nr5g_rsrp ? info.nr5g_rsrp + ' dBm' : ''],
    ['Mode reported by modem', info.network_type],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '');

  $('radio-rows').innerHTML = rows.map(([key, value]) =>
    `<div><span class="key">${key}</span><span class="val">${escapeHtml(value)}</span></div>`).join('');
}

// -- devices ----------------------------------------------------------------

function renderDevices(payload) {
  deviceStamp = (payload && payload.ts) || null;
  deviceClientIp = (payload && payload.client_ip) || null;
  deviceTrackedSince = (payload && payload.tracked_since) || null;
  deviceRange.windowSince = (payload && payload.range_since) || null;
  deviceRange.windowUntil = (payload && payload.range_until) || null;
  deviceItems = ((payload && payload.items) || []).slice().sort((a, b) =>
    (b.tracked_bytes || 0) - (a.tracked_bytes || 0));
  $('device-count').textContent = deviceItems.length;
  renderDeviceList();
}

function renderDeviceList() {
  $('devices-headline').textContent = lastErrors.router
    ? 'The indoor router is not answering'
      + (deviceStamp ? `, so this is the list as it was at ${clock(deviceStamp)}.`
        : ' and has not been reached yet.')
    : !deviceItems.length
      ? 'Nothing is connected.'
      : `${deviceItems.length} devices are on the WiFi right now.`;

  $('device-list').innerHTML = deviceItems.map((device) => `
    <tr data-mac="${escapeHtml(device.mac)}">
      <td class="dev-name">${escapeHtml(device.hostname || device.mac)}${
        device.ip && device.ip === deviceClientIp ? ' <span class="tag">This device</span>' : ''}</td>
      <td>${escapeHtml(device.ip)}</td>
      <td>${escapeHtml(device.band || 'WiFi')}</td>
      <td>${num(device.rssi)} dBm</td>
      <td class="live">${rate(device.down_speed)}</td>
      <td>${device.tracked_bytes === null || device.tracked_bytes === undefined
        ? 'not counted yet' : bytes(device.tracked_bytes)}</td>
    </tr>`).join('')
    || '<tr class="empty"><td colspan="6">Nothing is connected.</td></tr>';

  const partial = deviceTrackedSince && deviceRange.windowSince
    && deviceRange.windowSince < deviceTrackedSince
    ? ` Our own data only goes back to ${day(deviceTrackedSince)}, so this period is partial.`
    : '';
  $('th-used').querySelector('.th-info').dataset.tip =
    'This dashboard\'s own tally for the period picked above. '
    + 'Keeps counting across a reconnect, unlike the router\'s own total.' + partial;
}

function deviceRangeParams() {
  const params = new URLSearchParams();
  if (deviceRange.range === 'custom' && deviceRange.since) {
    params.set('range', 'custom');
    params.set('since', deviceRange.since);
    params.set('until', deviceRange.until || Math.floor(Date.now() / 1000));
  } else {
    params.set('range', deviceRange.range);
  }
  return params;
}

async function refreshDevices() {
  try {
    renderDevices(await getJson('/api/devices?' + deviceRangeParams()));
  } catch (err) { /* the next regular tick will retry */ }
}

let openEvent = null;   // ts of the row currently expanded
let eventsShown = 20;    // how many of the fetched events are on screen
const EVENTS_PAGE = 20;
let lastEvents = [];

function renderEvents(events) {
  lastEvents = events;
  const list = $('events');
  const more = $('events-more');
  if (!events.length) {
    list.innerHTML = '<li><span class="what foot">Nothing has happened yet — '
      + 'that is the good outcome.</span></li>';
    more.hidden = true;
    return;
  }
  const visible = events.slice(0, eventsShown);
  const remaining = events.length - visible.length;
  more.hidden = remaining <= 0;
  if (remaining > 0) more.textContent = `Show ${Math.min(EVENTS_PAGE, remaining)} more`;
  list.innerHTML = visible.map((event) => {
    const [severity, text, why] = EVENTS[event.kind] || ['', event.kind, ''];
    const open = openEvent === event.ts;
    return `<li class="${open ? 'open' : ''}" data-ts="${event.ts}"
              ${why ? 'data-why="1"' : ''}>
        <span class="sev ${severity}"></span>
        <time>${when(event.ts)}</time>
        <span class="what">${escapeHtml(text)}
          ${event.detail ? `<span class="foot">— ${escapeHtml(event.detail)}</span>` : ''}
          ${open && why ? `<span class="why">${escapeHtml(why)}</span>` : ''}</span>
        ${why ? `<span class="chev">${open ? '−' : '+'}</span>` : ''}
      </li>`;
  }).join('');

  list.querySelectorAll('li[data-why]').forEach((row) => {
    row.addEventListener('click', () => {
      const ts = Number(row.dataset.ts);
      openEvent = openEvent === ts ? null : ts;
      renderEvents(events);
    });
  });
}

$('events-more').addEventListener('click', () => {
  eventsShown += EVENTS_PAGE;
  renderEvents(lastEvents);
});

// -- charts -----------------------------------------------------------------

function pushLive(down, up, ts) {
  live.down.push(down);
  live.up.push(up);
  live.ts.push(ts || Date.now() / 1000);
  while (live.down.length > LIVE_POINTS) {
    live.down.shift(); live.up.shift(); live.ts.shift();
    if (live.hover !== null) live.hover--;
  }
  if (live.hover !== null && live.hover < 0) live.hover = null;
  try {
    localStorage.setItem('wifiapp-live',
      JSON.stringify({ down: live.down, up: live.up, ts: live.ts }));
  } catch { /* storage full or unavailable -- the chart still works, just won't persist */ }
  // Drawing while the Home view is hidden reads a 0x0 canvas box and bakes
  // that into the canvas's own width/height, which then stays 0 even after
  // the tab is shown again. Skip the draw entirely rather than corrupt it.
  if (!$('view-home').hidden) drawLive();
}

function surface(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

// Newest sample sits hard against the right edge, so the window is always the
// same five minutes wide however few points we have so far.
const PAD = { left: 44, right: 4, top: 10, bottom: 15 };

const liveX = (index, plot) =>
  plot.x + plot.w - (live.down.length - 1 - index) * (plot.w / (LIVE_POINTS - 1));

function area(ctx, values, plot, peak, colour, alpha) {
  if (values.length < 2) return;
  const y = (value) => plot.y + plot.h - (value / peak) * plot.h;

  ctx.beginPath();
  values.forEach((value, index) => {
    const x = liveX(index, plot);
    return index ? ctx.lineTo(x, y(value)) : ctx.moveTo(x, y(value));
  });
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1.75;
  ctx.stroke();

  ctx.lineTo(liveX(values.length - 1, plot), plot.y + plot.h);
  ctx.lineTo(liveX(0, plot), plot.y + plot.h);
  ctx.closePath();
  const gradient = ctx.createLinearGradient(0, plot.y, 0, plot.y + plot.h);
  gradient.addColorStop(0, alpha);
  gradient.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = gradient;
  ctx.fill();
}

let lastLivePlot = null;   // geometry from the most recent draw, for hover hit-testing

function drawLive() {
  const { ctx, width, height } = surface($('chart-live'));

  // A floor of 1 Mbps stops an idle line from being drawn as a mountain range.
  const peak = Math.max(...live.down, ...live.up, 125000);

  ctx.font = '10px system-ui, sans-serif';
  const yLabels = [1, 0.5, 0].map((fraction) => fraction ? rate(peak * fraction) : '0');
  // The widest label varies with the unit (kbps/Mbps/Gbps), so the left gutter
  // has to be measured, not guessed -- a fixed width clips whichever label
  // turns out wider than it assumed.
  const labelWidth = Math.max(...yLabels.map((text) => ctx.measureText(text).width));
  const padLeft = Math.max(PAD.left, Math.ceil(labelWidth) + 12);

  const plot = { x: padLeft, y: PAD.top,
                 w: width - padLeft - PAD.right, h: height - PAD.top - PAD.bottom };
  lastLivePlot = plot;

  ctx.textBaseline = 'middle';
  [1, 0.5, 0].forEach((fraction, i) => {
    const y = plot.y + plot.h * (1 - fraction);
    ctx.strokeStyle = 'rgba(255,255,255,.07)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plot.x, y + 0.5);
    ctx.lineTo(plot.x + plot.w, y + 0.5);
    ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,.32)';
    ctx.textAlign = 'right';
    ctx.fillText(yLabels[i], plot.x - 6, y);
  });

  area(ctx, live.up, plot, peak, 'rgba(255,255,255,.35)', 'rgba(255,255,255,.06)');
  area(ctx, live.down, plot, peak, '#3b82f6', 'rgba(59,130,246,.28)');

  ctx.fillStyle = 'rgba(255,255,255,.32)';
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  ctx.fillText('3 min ago', plot.x, height - 3);
  ctx.textAlign = 'right';
  ctx.fillText('now', plot.x + plot.w, height - 3);
  ctx.textAlign = 'left';

  drawLiveHover(ctx, plot, peak);
}

function drawLiveHover(ctx, plot, peak) {
  const tip = $('live-tip');
  const index = live.hover;
  if (index === null || index === undefined || live.down[index] === undefined) {
    tip.hidden = true;
    return;
  }

  const x = liveX(index, plot);
  const y = plot.y + plot.h - (live.down[index] / peak) * plot.h;

  ctx.strokeStyle = 'rgba(255,255,255,.25)';
  ctx.beginPath();
  ctx.moveTo(x + 0.5, plot.y);
  ctx.lineTo(x + 0.5, plot.y + plot.h);
  ctx.stroke();
  ctx.fillStyle = '#3b82f6';
  ctx.beginPath();
  ctx.arc(x, y, 3, 0, Math.PI * 2);
  ctx.fill();

  const ago = Math.max(0, Math.round(Date.now() / 1000 - live.ts[index]));
  tip.hidden = false;
  tip.innerHTML = `<b>↓ ${rate(live.down[index])}</b> &nbsp; ↑ ${rate(live.up[index])}`
    + `<br><span>${ago < 2 ? 'just now' : ago + ' seconds ago'}</span>`;
  tip.style.left = clampTip(x, tip) + 'px';
  tip.style.top = Math.round(Math.max(plot.y + 20, y - 8)) + 'px';
}

// The bubble is centred on the point, so it needs half its own width of room on
// either side or it hangs off the card.
function clampTip(x, tip) {
  const half = tip.offsetWidth / 2 + 2;
  const width = tip.parentNode.clientWidth;
  return Math.round(Math.min(Math.max(x, half), width - half));
}

function liveHoverFrom(event) {
  const canvas = $('chart-live');
  const box = canvas.getBoundingClientRect();
  const plotX = lastLivePlot ? lastLivePlot.x : PAD.left;
  const plotW = box.width - plotX - PAD.right;
  const step = plotW / (LIVE_POINTS - 1);
  const offset = Math.round((plotX + plotW - (event.clientX - box.left)) / step);
  const index = live.down.length - 1 - offset;
  return (index >= 0 && index < live.down.length) ? index : null;
}

['mousemove', 'touchmove'].forEach((name) => {
  $('chart-live').addEventListener(name, (event) => {
    const point = event.touches ? event.touches[0] : event;
    live.hover = liveHoverFrom(point);
    drawLive();
  }, { passive: true });
});

['mouseleave', 'touchend'].forEach((name) => {
  $('chart-live').addEventListener(name, () => { live.hover = null; drawLive(); });
});

const isToday = (ts) =>
  new Date(ts * 1000).toDateString() === new Date().toDateString();

// How each bucket is labelled on the axis, and how one bar is named in words --
// always with the actual clock time or date, never just "this hour".
const BUCKET = {
  '5min': {
    each: 'five minutes',
    tick: (ts) => clock(ts),
    span: (ts) => `${clock(ts)}–${clock(ts + 300)}`,
  },
  hour: {
    each: 'hour',
    tick: (ts) => (isToday(ts) ? '' : day(ts) + ' ') + clock(ts),
    span: (ts) => (isToday(ts) ? '' : day(ts) + ', ')
      + `${clock(ts)}–${clock(ts + 3600)}`,
  },
  day: {
    each: 'day',
    tick: (ts) => day(ts),
    span: (ts) => new Date(ts * 1000)
      .toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'short' }),
  },
  week: {
    each: 'week',
    tick: (ts) => day(ts),
    span: (ts) => `Week of ${new Date(ts * 1000)
      .toLocaleDateString([], { day: 'numeric', month: 'short' })}`,
  },
  month: {
    each: 'month',
    tick: (ts) => new Date(ts * 1000).toLocaleDateString([], { month: 'short', year: '2-digit' }),
    span: (ts) => new Date(ts * 1000).toLocaleDateString([], { month: 'long', year: 'numeric' }),
  },
};

const RANGE_TITLE = {
  hour: 'Last hour', day: 'Last 24 hours',
  week: 'Last 7 days', month: 'Last 30 days', cycle: 'This billing cycle',
  all: 'All time', custom: 'Custom range',
};

// What to call the equal-length window right before the selected one, per range.
const PREV_RANGE_LABEL = {
  hour: 'previous hour', day: 'previous 24h',
  week: 'previous 7 days', month: 'previous 30 days',
  cycle: 'previous cycle', custom: 'previous period',
};

// The title of the chart, with the period written out.
function rangeLabel(range, since, until) {
  const now = Date.now() / 1000;
  if (range === 'hour') return `Last hour · ${clock(since)} to ${clock(now)}`;
  if (range === 'day') return `Last 24 hours · ${when(since)} to now`;
  if (range === 'week') return `Last 7 days · ${day(since)} to ${day(now)}`;
  if (range === 'month') return `Last 30 days · ${day(since)} to ${day(now)}`;
  if (range === 'cycle') {
    return cycleInfo ? `This cycle · ${cycleWindow(cycleInfo)}` : 'This cycle';
  }
  if (range === 'all') return `All time · since ${day(since)}`;
  if (range === 'custom') return `${day(since)} to ${day(until || now)}`;
  return 'Usage';
}

function drawUsage() {
  const canvas = $('chart-usage');
  const { ctx, width, height } = surface(canvas);
  const points = usage.points;
  const floor = height - 16;

  if (!points.length) {
    $('usage-tip').hidden = true;
    ctx.fillStyle = 'rgba(255,255,255,.3)';
    ctx.font = '12px system-ui, sans-serif';
    ctx.fillText('No usage texts stored for this period.', 0, height / 2);
    return;
  }

  const values = points.map((p) => p.down + p.up);
  const peak = Math.max(...values, 1);
  const slot = width / values.length;

  // Value labels only render where they'd actually fit: wide enough for the
  // text, and above the plot's top edge. With many thin bars this quietly
  // stops drawing them rather than piling up unreadable overlapping text.
  ctx.font = '10px system-ui, sans-serif';
  const labelWidth = ctx.measureText('000.0 GB').width;
  const showLabels = slot >= labelWidth + 6;

  values.forEach((value, index) => {
    const barHeight = (value / peak) * (floor - 12);
    const lit = usage.selected === index || usage.hover === index;
    const barX = index * slot + slot * 0.15;
    const barW = Math.max(slot * 0.7, 1);
    ctx.fillStyle = lit ? '#fff' : '#3b82f6';
    ctx.fillRect(barX, floor - barHeight, barW, Math.max(barHeight, 1));

    if (showLabels && value > 0) {
      ctx.fillStyle = lit ? 'rgba(255,255,255,.9)' : 'rgba(255,255,255,.5)';
      ctx.textAlign = 'center';
      ctx.fillText(bytes(value), barX + barW / 2, Math.max(20, floor - barHeight - 4));
      ctx.textAlign = 'left';
    }
  });

  positionUsageTip(slot, floor, values, peak);

  ctx.fillStyle = 'rgba(255,255,255,.3)';
  ctx.font = '10px system-ui, sans-serif';
  ctx.fillText(bytes(peak), 0, 9);
  ctx.fillText(BUCKET[usage.bucket].tick(points[0].ts), 0, height - 3);
  ctx.textAlign = 'right';
  ctx.fillText(BUCKET[usage.bucket].tick(points[points.length - 1].ts),
               width, height - 3);
  ctx.textAlign = 'left';
}

function describeSelection() {
  const hint = $('chart-hint');
  const point = usage.points[usage.selected];
  if (!point) {
    hint.textContent = usage.points.length
      ? `Tap a bar to read it. Each bar is one ${BUCKET[usage.bucket].each}.`
      : 'Airtel has not texted a total for any day in this period.';
    return;
  }
  const span = BUCKET[usage.bucket].span(point.ts);
  hint.textContent = `${span} — ${bytes(point.down)}, as billed by Airtel.`;
}

function positionUsageTip(slot, floor, values, peak) {
  const tip = $('usage-tip');
  const index = usage.hover;
  const point = index === null ? null : usage.points[index];
  if (!point) { tip.hidden = true; return; }

  tip.hidden = false;
  tip.innerHTML = `<b>${bytes(point.down)}</b>`
    + `<br><span>${BUCKET[usage.bucket].span(point.ts)}</span>`;
  tip.style.left = clampTip(index * slot + slot / 2, tip) + 'px';
  tip.style.top = Math.round(Math.max(14, floor - (values[index] / peak) * (floor - 12) - 6))
    + 'px';
}

const usageIndexFrom = (event) => {
  if (!usage.points.length) return null;
  const box = $('chart-usage').getBoundingClientRect();
  const index = Math.floor((event.clientX - box.left) / (box.width / usage.points.length));
  return (index >= 0 && index < usage.points.length) ? index : null;
};

$('chart-usage').addEventListener('click', (event) => {
  const index = usageIndexFrom(event);
  usage.selected = (index === null || index === usage.selected) ? null : index;
  drawUsage();
  describeSelection();
});

$('chart-usage').addEventListener('mousemove', (event) => {
  const index = usageIndexFrom(event);
  if (index === usage.hover) return;
  usage.hover = index;
  drawUsage();
}, { passive: true });

$('chart-usage').addEventListener('mouseleave', () => {
  usage.hover = null;
  drawUsage();
});

// -- data plumbing ----------------------------------------------------------

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(data.error || response.statusText);
    err.status = response.status;
    err.field = data.field;
    throw err;
  }
  return data;
}

async function loadUsage() {
  const params = new URLSearchParams();
  if (usage.range === 'custom' && usage.since) {
    params.set('range', 'custom');
    params.set('since', usage.since);
    params.set('until', usage.until || Math.floor(Date.now() / 1000));
  } else {
    params.set('range', usage.range);
  }
  if (usage.group) params.set('group', usage.group);
  params.set('source', 'carrier');

  try {
    const series = await getJson('/api/usage/series?' + params.toString());
    usage.points = series.points || [];
    usage.bucket = series.bucket;
    usage.selected = null;
    $('chart-title').textContent = rangeLabel(usage.range, series.since, series.until);
    $('chart-total').textContent = bytes(series.total);
    $('chart-delta').innerHTML = pctChange(series.total, series.prev_total,
      PREV_RANGE_LABEL[usage.range] || 'previous period');
    drawUsage();
    describeSelection();
  } catch (err) {
    $('chart-total').textContent = '—';
    $('chart-delta').textContent = '';
    $('chart-hint').textContent = err.message;
  }

  if (usage.range === 'custom') {
    $('devices-sub').textContent = 'Custom range';
    $('usage-devices').innerHTML = '<div><span class="key">Per-device breakdown isn\'t '
      + 'available for custom ranges yet.</span></div>';
    return;
  }
  loadUsageDevices();
}

function csvTimestamp(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 19).replace('T', ' ');
}

$('usage-export').addEventListener('click', () => {
  if (!usage.points.length) {
    $('chart-hint').textContent = 'Nothing to export for this range yet.';
    return;
  }
  const rows = [['Source: Airtel SMS'], ['Date', 'Download (bytes)', 'Upload (bytes)']];
  usage.points.forEach((p) => rows.push([csvTimestamp(p.ts), p.down || 0, p.up || 0]));
  const csv = rows.map((row) => row.join(',')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `wifiapp-usage-airtel-sms-${usage.range}-${usage.bucket}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
});

function selectRange(range) {
  usage.range = range;
  $('range-select').value = range;
  $('custom-range').hidden = range !== 'custom';
}

// -- connection settings ----------------------------------------------------

const PDP = { 1: 'IPv4 only', 2: 'IPv6 only', 3: 'IPv4 and IPv6' };
const AUTH = { 0: 'none', 1: 'PAP', 2: 'CHAP', 3: 'PAP or CHAP' };

let networkConfig = null;

async function loadNetworkConfig() {
  if (networkConfig) return;                 // it only changes when changed
  try {
    networkConfig = await getJson('/api/network-config');
  } catch (err) {
    $('apn-note').textContent = 'Could not read the connection settings: ' + err.message;
    return;
  }
  renderNetworkConfig(networkConfig);
}

let phoneQrDone = false;
async function loadPhoneQr() {
  if (phoneQrDone) return;                    // the LAN address doesn't change mid-session
  phoneQrDone = true;
  try {
    const { url } = await getJson('/api/lan-url');
    if (!url) throw new Error('no network address found');
    QRCode.renderCanvas($('phone-qr'), url);
    $('phone-qr-note').textContent = url;
  } catch (err) {
    $('phone-card').hidden = true;
  }
}

function renderNetworkConfig(config) {
  const apn = config.apn || {};
  const active = apn.active || {};
  const auto = (apn.automatic || [])[0] || {};

  const rows = [
    ['Access point (APN)', active.wanapn || '—'],
    ['Profile name', active.profilename || '—'],
    ['Chosen', apn.mode === 'manual' ? 'by hand' : 'automatically, by network'],
    ['Address type', PDP[active.pdpType] || '—'],
    ['Sign-in', AUTH[active.pppAuthMode] || '—'],
  ];

  $('apn-sub').textContent = `${active.wanapn || 'no APN'} · ${apn.mode}`;
  $('apn-rows').innerHTML = rows.map(([key, value]) =>
    `<div><span class="key">${key}</span>`
    + `<span class="val">${escapeHtml(value)}</span></div>`).join('');

  $('apn-note').innerHTML =
    `Dialing <b>${escapeHtml(active.wanapn || '—')}</b> by hand — left alone, `
    + `the unit would use <b>${escapeHtml(auto.wanapn || 'its own table')}</b>. `
    + 'Changing it drops the connection until it reconnects, with an automatic '
    + 'revert if the link doesn\'t come back.';

  $('apn-input').value = active.wanapn || '';
  $('apn-input').placeholder = 'APN, e.g. airtelng';
  $('apn-username').value = active.username || '';

  $('apn-save').disabled = !writesEnabled;
}

$('apn-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const apn = $('apn-input').value.trim();
  if (!apn) return;
  if (!confirm(`Change the APN to "${apn}"?\n\nThe connection drops while the modem `
    + 're-dials. If it does not come back within a couple of minutes, the previous '
    + 'APN is restored automatically.')) return;

  const button = $('apn-save');
  button.disabled = true;
  $('apn-save-note').textContent = 'Saving… the connection will drop briefly.';
  try {
    await getJson('/api/apn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        apn,
        username: $('apn-username').value.trim(),
        password: $('apn-password').value,
      }),
    });
    $('apn-save-note').textContent = 'Saved. Reconnecting…';
    $('apn-password').value = '';
    networkConfig = null;
    await loadNetworkConfig();
  } catch (err) {
    $('apn-save-note').textContent = err.message;
  } finally {
    button.disabled = !writesEnabled;
  }
});

// -- messages ---------------------------------------------------------------

let messages = [];
let smsFilter = '';
let smsThread = null;          // sender key of the open conversation, or null

async function loadSms(refresh) {
  try {
    const data = await getJson('/api/sms' + (refresh ? '?refresh=1' : ''));
    messages = data.messages || [];
    renderSim(data);
    renderSms();
  } catch (err) {
    $('sms-headline').textContent = err.message;
  }
}

const threadKey = (message) => message.from || '';

function renderSim(data) {
  const unread = data.unread || 0;
  $('sms-headline').textContent = messages.length
    ? `${messages.length} messages on the outdoor unit`
      + (unread ? `, ${unread} unread.` : '.')
    : 'No messages stored on the outdoor unit.';

  const badge = $('sms-badge');
  badge.hidden = !unread;
  badge.textContent = unread;

  $('sim-rows').innerHTML = [
    ['Phone number', data.number || 'not known yet'],
    ['Stored messages', `${messages.length} of about 100`],
  ].map(([key, value]) =>
    `<div><span class="key">${key}</span><span class="val">${escapeHtml(value)}</span></div>`)
    .join('');
}

let smsPicking = false;
const smsPicked = new Set();

const smsShown = () => messages.filter((message) =>
  smsFilter === 'unread' ? message.unread
    : smsFilter === 'other' ? !message.usage : true);

function buildThreads(pool) {
  const map = new Map();
  for (const message of pool) {
    const key = threadKey(message);
    (map.get(key) || map.set(key, []).get(key)).push(message);
  }
  return [...map.values()].map((msgs) => {
    msgs.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    return { from: threadKey(msgs[0]), messages: msgs, latest: msgs[0],
             unread: msgs.filter((m) => m.unread).length };
  }).sort((a, b) => (b.latest.ts || 0) - (a.latest.ts || 0));
}

function renderSms() {
  // A thread that no longer has any messages (all deleted) can't stay open.
  if (smsThread && !messages.some((m) => threadKey(m) === smsThread)) smsThread = null;

  $('sms-thread-header').hidden = !smsThread;
  $('sms-toolbar').hidden = !smsThread;
  $('sms-filter').hidden = !!smsThread;
  if (smsThread) $('sms-thread-title').textContent = smsThread || 'Unknown';

  const shown = smsShown();
  if (smsThread) {
    renderThread(shown.filter((m) => threadKey(m) === smsThread));
  } else {
    renderThreadList(buildThreads(shown));
  }
}

function renderThreadList(threads) {
  $('sms-list').innerHTML = threads.map((t) => `
    <article class="device sms" data-from="${escapeHtml(t.from)}">
      <div class="sms-top">
        <span class="sms-from">${escapeHtml(t.from || 'Unknown')}${
          t.unread ? `<span class="badge">${t.unread}</span>` : ''}</span>
        <span class="sms-when">${t.latest.ts ? when(t.latest.ts) : ''}</span>
      </div>
      <p class="sms-body sms-preview">${escapeHtml(t.latest.body)}</p>
      ${t.latest.usage ? '<span class="sms-tag">Daily usage alert</span>' : ''}
    </article>`).join('')
    || '<p class="foot">Nothing matches this filter.</p>';

  $('sms-list').querySelectorAll('[data-from]').forEach((node) => {
    node.addEventListener('click', () => openThread(node.dataset.from));
  });
}

function renderThread(msgs) {
  msgs.sort((a, b) => (b.ts || 0) - (a.ts || 0));

  $('sms-list').innerHTML = msgs.map((message) => `
    <article class="device sms${message.unread ? ' unread' : ''}${
      smsPicking ? ' pick' : ''}${smsPicked.has(message.id) ? ' picked' : ''}"
      data-id="${message.id}">
      ${smsPicking ? '<span class="sms-check"></span>' : ''}
      <div class="sms-top">
        <span class="sms-when">${message.ts ? when(message.ts) : ''}</span>
      </div>
      <p class="sms-body">${escapeHtml(message.body)}</p>
      ${message.usage ? '<span class="sms-tag">Daily usage alert</span>' : ''}
    </article>`).join('')
    || '<p class="foot">Nothing matches this filter.</p>';

  if (smsPicking) {
    $('sms-list').querySelectorAll('[data-id]').forEach((node) => {
      node.addEventListener('click', () => {
        const id = Number(node.dataset.id);
        if (smsPicked.has(id)) smsPicked.delete(id); else smsPicked.add(id);
        renderSms();
      });
    });
  }

  renderSmsToolbar(msgs);
}

async function openThread(from) {
  smsThread = from;
  smsPicking = false;
  smsPicked.clear();
  renderSms();

  const unread = messages.filter((m) => threadKey(m) === from && m.unread);
  if (!unread.length) return;
  unread.forEach((m) => { m.unread = false; });
  renderSms();
  try {
    await getJson('/api/sms/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: unread.map((m) => m.id) }),
    });
  } catch (err) { /* ignored -- the next load reflects whatever really happened */ }
  loadSms(true);
}

$('sms-back').addEventListener('click', () => {
  smsThread = null;
  smsPicking = false;
  smsPicked.clear();
  renderSms();
});

function renderSmsToolbar(shown) {
  const count = smsPicked.size;
  $('sms-select').textContent = smsPicking ? 'Done' : 'Select';
  $('sms-selected').textContent = smsPicking
    ? (count ? `${plural(count, 'message')} selected` : 'Tap messages to select them')
    : '';
  ['sms-all', 'sms-mark', 'sms-delete'].forEach((id) => { $(id).hidden = !smsPicking; });
  $('sms-all').textContent =
    count && count >= shown.length ? 'None' : 'All';
  $('sms-mark').disabled = !count;
  $('sms-delete').disabled = !count;
}

function stopPicking() {
  smsPicking = false;
  smsPicked.clear();
  renderSms();
}

$('sms-select').addEventListener('click', () => {
  smsPicking = !smsPicking;
  smsPicked.clear();
  renderSms();
});

$('sms-all').addEventListener('click', () => {
  const shown = smsShown().filter((m) => threadKey(m) === smsThread);
  if (smsPicked.size >= shown.length) smsPicked.clear();
  else shown.forEach((message) => smsPicked.add(message.id));
  renderSms();
});

$('sms-mark').addEventListener('click', async () => {
  const ids = [...smsPicked];
  $('sms-headline').textContent = 'Marking as read…';
  try {
    await getJson('/api/sms/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
  } catch (err) {
    $('sms-headline').textContent = err.message;
    return;
  }
  stopPicking();
  loadSms(true);
});

$('sms-delete').addEventListener('click', async () => {
  const ids = [...smsPicked];
  if (!confirm(`Delete ${plural(ids.length, 'message')} from the outdoor unit?\n\n`
    + 'This removes them from the SIM itself and cannot be undone. Any usage '
    + 'figures already read out of them stay in this app.')) return;

  $('sms-headline').textContent = 'Deleting…';
  try {
    // One at a time: the firmware takes a single id per call.
    for (const id of ids) {
      await getJson('/api/sms/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
    }
  } catch (err) {
    $('sms-headline').textContent = err.message;
  }
  stopPicking();
  loadSms(true);
});

function when(ts) {
  const date = new Date(ts * 1000);
  const sameDay = new Date().toDateString() === date.toDateString();
  return sameDay ? clock(ts) : `${day(ts)}, ${clock(ts)}`;
}

document.querySelectorAll('#sms-filter button').forEach((button) => {
  button.addEventListener('click', () => {
    smsFilter = button.dataset.sms;
    document.querySelectorAll('#sms-filter button').forEach((other) =>
      other.classList.toggle('on', other === button));
    renderSms();
  });
});

async function loadUsageDevices() {
  try {
    const rows = await getJson('/api/usage/devices?range=' + usage.range);
    renderUsageDevices(rows);
  } catch (err) { /* the panel stays as it was */ }
}

function renderUsageDevices(rows) {
  const container = $('usage-devices');
  $('devices-sub').textContent = RANGE_TITLE[usage.range] || '';

  if (!rows.length) {
    container.innerHTML = '<div><span class="key">No per-device traffic recorded '
      + 'for this period.</span></div>';
    return;
  }

  const peak = Math.max(1, ...rows.map((r) => r.down + r.up));
  container.innerHTML = rows.slice(0, 10).map((row) => {
    const used = row.down + row.up;
    return `<div>
        <span class="key">${escapeHtml(row.hostname)}${
          row.you ? ' <span class="tag">This device</span>' : ''}</span>
        <span class="meter"><span class="bar"><span class="fill"
          style="width:${(used / peak * 100).toFixed(1)}%"></span></span></span>
        <span class="val">${bytes(used)}</span>
      </div>`;
  }).join('');
}

$('range-select').addEventListener('change', () => {
  selectRange($('range-select').value);
  if (usage.range === 'custom') return;  // wait for the user to pick dates and press Apply
  loadUsage();
});

$('range-apply').addEventListener('click', () => {
  const sinceVal = $('range-since').value;
  const untilVal = $('range-until').value;
  if (!sinceVal) return;
  usage.since = Math.floor(new Date(sinceVal + 'T00:00:00').getTime() / 1000);
  usage.until = untilVal
    ? Math.floor(new Date(untilVal + 'T23:59:59').getTime() / 1000)
    : Math.floor(Date.now() / 1000);
  loadUsage();
});

document.querySelectorAll('#group-picker button').forEach((button) => {
  button.addEventListener('click', () => {
    usage.group = button.dataset.group;
    document.querySelectorAll('#group-picker button').forEach((other) =>
      other.classList.toggle('on', other === button));
    loadUsage();
  });
});

$('device-range-select').addEventListener('change', () => {
  deviceRange.range = $('device-range-select').value;
  $('device-custom-range').hidden = deviceRange.range !== 'custom';
  if (deviceRange.range === 'custom') return;  // wait for the user to press Apply
  refreshDevices();
});

$('device-range-apply').addEventListener('click', () => {
  const sinceVal = $('device-range-since').value;
  const untilVal = $('device-range-until').value;
  if (!sinceVal) return;
  deviceRange.since = Math.floor(new Date(sinceVal + 'T00:00:00').getTime() / 1000);
  deviceRange.until = untilVal
    ? Math.floor(new Date(untilVal + 'T23:59:59').getTime() / 1000)
    : Math.floor(Date.now() / 1000);
  refreshDevices();
});

// Native title tooltips don't show on a tap, and this dashboard is mostly
// used on a phone -- so column-header help is a tap-to-open popover instead.
let openThTip = null;
function closeThTip() {
  if (openThTip) { openThTip.remove(); openThTip = null; }
}
document.querySelector('.dev-table thead').addEventListener('click', (event) => {
  const button = event.target.closest('.th-info');
  if (!button) return;
  event.stopPropagation();
  const already = openThTip && openThTip.forButton === button;
  closeThTip();
  if (already) return;
  const tip = document.createElement('div');
  tip.className = 'th-tip';
  tip.textContent = button.dataset.tip;
  tip.forButton = button;
  document.body.appendChild(tip);
  const r = button.getBoundingClientRect();
  const left = Math.min(r.left, window.innerWidth - tip.offsetWidth - 12);
  tip.style.left = Math.max(12, left) + 'px';
  tip.style.top = (r.bottom + 8) + 'px';
  openThTip = tip;
});
document.addEventListener('click', closeThTip);
document.addEventListener('scroll', closeThTip, true);

let ticks = 0;
let tickBusy = false;

async function tick() {
  // A phone backgrounding the page can leave a fetch hanging for a long time;
  // without this, resuming fires a pile of overlapping requests instead of
  // one that just picks up where it left off.
  if (!loggedIn || tickBusy) return;
  tickBusy = true;
  try {
    renderOverview(await getJson('/api/overview'));
    renderDevices(await getJson('/api/devices?' + deviceRangeParams()));
    renderEvents(await getJson('/api/events'));
    // Only for the unread badge; the messages themselves are read on demand.
    if (ticks++ % 12 === 0) loadSms();
  } catch (err) {
    if (err.status === 401) {
      showLogin();
      return;
    }
    const banner = $('banner');
    banner.hidden = false;
    banner.textContent = 'Cannot reach the dashboard server: ' + err.message;
  } finally {
    tickBusy = false;
  }
}

// -- actions ----------------------------------------------------------------

async function switchMode(mode) {
  if (mode === currentMode) return;
  const [name] = splitLabel(modes[mode] || mode);
  if (!confirm(`Switch to ${name}?\n\nThe connection drops for 10–30 seconds. `
    + 'If it does not come back, the previous mode is restored automatically.')) return;
  $('mode-note').textContent = 'Switching… the connection will drop briefly.';
  try {
    await getJson('/api/network-mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
  } catch (err) {
    $('mode-note').textContent = err.message;
  }
}

// Picks a concrete network mode for the chosen goal, using the last signal
// reading. 5G NSA (LTE_AND_5G) only helps when it is itself healthy; when it
// is weak or noisy, plain LTE is both faster in practice and steadier, since
// there is no 5G layer dropping in and out on top of it.
function pickModeFor(goal) {
  const { strength, clarity } = lastSignal;
  const healthy5g = strength !== null && clarity !== null
    && strength >= -95 && clarity >= 13;

  if (goal === 'game') return 'Only_LTE';
  if (goal === 'performance') return healthy5g ? 'LTE_AND_5G' : 'Only_LTE';
  return 'WL_AND_5G';
}

const GOAL_LABEL = { default: 'Default', game: 'Game', performance: 'Performance' };

function markOptimise(goal) {
  optimiseMode = goal;
  document.querySelectorAll('#optimise-picker button').forEach((button) =>
    button.classList.toggle('on', button.dataset.opt === goal));
  $('optimise-text').textContent = GOAL_LABEL[goal] || 'Optimise';
}

// QoS Intelligent Allocation priority for each goal (router_set_qos's
// qos_smart_pri_type: 0 automatic, 1 game, 2 web page, 3 video).
const QOS_FOR_GOAL = {
  default: { enable: false, priority: 0 },
  game: { enable: true, priority: 1 },
  performance: { enable: true, priority: 3 },
};

// What each goal does, and when to pick it — kept to one short line each
// so the confirm popup stays readable. Performance's reason is worked out at
// confirm time instead, since whether 5G is actually available right now
// changes what's true (see applyOptimise).
const REASON_FOR_GOAL = {
  default: 'Auto radio, QoS off — use for everyday browsing.',
  game: '4G-only + game-priority traffic — use for gaming and calls.',
};

async function applyOptimise(goal) {
  const target = pickModeFor(goal);
  const sameMode = target === currentMode;
  const [name] = splitLabel(modes[target] || target);
  // Performance asks for 5G, but only gets it when the current signal is
  // healthy enough -- otherwise pickModeFor() already fell back to 4G-only,
  // same radio as Game. Say so, rather than always claiming "best radio".
  const reason = goal === 'performance'
    ? target === 'Only_LTE'
      ? 'Signal too weak for 5G right now, so this falls back to 4G-only — still throughput priority.'
      : '5G + throughput priority — use for downloads and streaming.'
    : REASON_FOR_GOAL[goal] || '';

  if (!sameMode) {
    // Leave the toggle where it was until the switch is actually confirmed,
    // so cancelling doesn't strand the button on a mode that was never applied.
    if (!confirm(`Switch to ${name} for ${goal}?\n${reason}\n\n`
      + 'The connection drops for 10–30 seconds. If it does not come back, '
      + 'the previous mode is restored automatically.')) return;
  }

  markOptimise(goal);
  $('optimise-note').textContent = sameMode
    ? 'Applying…' : 'Switching… the connection will drop briefly.';
  try {
    if (!sameMode) {
      await getJson('/api/network-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: target }),
      });
    }
    const qos = QOS_FOR_GOAL[goal] || QOS_FOR_GOAL.default;
    await getJson('/api/qos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(qos),
    });
    $('optimise-note').textContent = `Optimised for ${goal}: ${name}`
      + (qos.enable ? ', QoS prioritised.' : '.');
  } catch (err) {
    $('optimise-note').textContent = err.message;
  }
}

document.querySelectorAll('#optimise-picker button').forEach((button) => {
  button.addEventListener('click', () => applyOptimise(button.dataset.opt));
});

$('billing-day-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const day = Number($('billing-day-input').value);
  if (!Number.isInteger(day) || day < 1 || day > 28) {
    $('billing-day-note').textContent = 'Pick a day between 1 and 28.';
    return;
  }
  try {
    await getJson('/api/billing-day', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ day }),
    });
    $('billing-day-note').textContent = `Saved — cycles now start on day ${day}.`;
  } catch (err) {
    $('billing-day-note').textContent = err.message;
  }
});

$('toggle-reset').addEventListener('click', async (event) => {
  if (!autoReset) return;
  const turningOn = !autoReset.enabled;
  if (!confirm(turningOn
    ? 'Let the outdoor unit clear its own data counters every month?\n\n'
      + 'This makes its month figure a real billing cycle. It does not affect '
      + "this dashboard's own count."
    : 'Stop the outdoor unit clearing its counters each month?')) return;

  const button = event.currentTarget;
  button.disabled = true;
  try {
    await getJson('/api/auto-reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: turningOn }),
    });
    $('reset-note').textContent = 'Saved — the change shows here within a few minutes.';
  } catch (err) {
    $('reset-note').textContent = err.message;
    button.disabled = false;
  }
});

$('hero-number').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  try {
    await navigator.clipboard.writeText(button.textContent.replace(/\s/g, ''));
    button.classList.add('copied');
    setTimeout(() => button.classList.remove('copied'), 1200);
  } catch (err) { /* clipboard is blocked outside https — nothing to do */ }
});

$('activity-help').addEventListener('click', () => {
  const panel = $('activity-explain');
  panel.hidden = !panel.hidden;
});

$('run-diag').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = 'Testing…';
  try {
    const hops = await getJson('/api/diagnostics');
    $('diag-rows').innerHTML = hops.map((hop) =>
      `<div><span class="key">${escapeHtml(hop.label)}</span>`
      + `<span class="val">${hop.ms === null ? 'no reply' : hop.ms + ' ms'}</span></div>`).join('');
  } catch (err) {
    $('diag-rows').innerHTML = `<div><span class="key">${escapeHtml(err.message)}</span></div>`;
  } finally {
    button.disabled = false;
    button.textContent = 'Connection test';
  }
});

$('run-speed').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = 'Downloading…';
  $('speed-note').textContent = 'Running — this takes a few seconds.';
  try {
    const result = await getJson('/api/speedtest?mb=10');
    $('speed-note').textContent =
      `${result.mbps} Mbps — ${bytes(result.bytes)} in ${result.seconds}s. `
      + 'Measured from this PC, so WiFi and the PC itself are part of the result.';
  } catch (err) {
    $('speed-note').textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = 'Speed test';
  }
});

document.querySelectorAll('[data-reboot]').forEach((button) => {
  button.addEventListener('click', async () => {
    const target = button.dataset.reboot;
    const label = { odu: 'the outdoor unit', router: 'the indoor router',
                    both: 'both the router and the outdoor unit' }[target];
    const detail = target === 'both'
      ? 'The router restarts first, then the outdoor unit, so there is a LAN '
        + 'ready by the time the mobile link returns. Expect three to five '
        + 'minutes with nothing working — including this dashboard.'
      : target === 'router'
        ? 'WiFi disappears for a minute or two. The mobile link itself stays up.'
        : 'The mobile link goes down for a few minutes. WiFi stays on, but '
          + 'nothing on it can reach the internet.';
    if (!confirm(`Restart ${label}?\n\n${detail}`)) return;
    try {
      await getJson('/api/reboot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target }),
      });
      button.textContent = 'Restarting…';
    } catch (err) {
      alert(err.message);
    }
  });
});

$('logout-btn').addEventListener('click', async () => {
  if (!confirm('Log out of this dashboard?')) return;
  try {
    await getJson('/api/logout', { method: 'POST' });
  } catch (err) { /* the cookie is being cleared either way -- show the login screen */ }
  showLogin();
});

/* The speeds have their own loop. Both devices answer in tens of milliseconds,
   so a reading a second costs almost nothing and makes the numbers feel live
   instead of stepping every five seconds. */
let liveBusy = false;

async function tickLive() {
  // A phone backgrounding the page can leave a fetch hanging for a long time;
  // without this, resuming fires a pile of overlapping requests instead of
  // one that just picks up where it left off.
  if (!loggedIn || liveBusy) return;
  liveBusy = true;
  try {
    let data;
    try {
      data = await getJson('/api/live');
    } catch (err) {
      if (err.status === 401) showLogin();
      return;                       // the slow loop owns the error banner
    }
    // The router half can still be good while the outdoor unit is away, so
    // the device speeds are applied either way.
    updateDeviceSpeeds(data.devices || []);

    if (data.down_speed === null || data.down_speed === undefined) {
      $('down-speed').textContent = '—';
      $('up-speed').textContent = '—';
      return;
    }

    $('down-speed').textContent = rate(data.down_speed);
    $('up-speed').textContent = rate(data.up_speed);
    pushLive(data.down_speed || 0, data.up_speed || 0, data.ts);
  } finally {
    liveBusy = false;
  }
}

function updateDeviceSpeeds(rows) {
  const byMac = new Map(rows.map((row) => [row.mac, row]));
  deviceItems.forEach((device) => {
    const row = byMac.get(device.mac);
    if (!row) return;
    device.down_speed = row.down_speed;
    device.up_speed = row.up_speed;
  });
  document.querySelectorAll('#device-list [data-mac]').forEach((node) => {
    const row = byMac.get(node.dataset.mac);
    if (row) node.querySelector('.live').textContent = rate(row.down_speed);
  });
}

window.addEventListener('resize', () => {
  if (!$('view-home').hidden) drawLive();
  if (!$('view-usage').hidden) drawUsage();
});

// A backgrounded phone throttles or fully pauses these intervals -- for how
// long is not knowable -- so coming back needs an immediate catch-up rather
// than waiting for whatever is next on the clock, and the charts need an
// explicit redraw since they were sized 0 for however long the tab was away.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  tick();
  tickLive();
  if (!$('view-home').hidden) drawLive();
  if (!$('view-usage').hidden) drawUsage();
});

// -- login --------------------------------------------------------------

const VIEWS = ['home', 'usage', 'devices', 'sms', 'settings'];

function showLogin() {
  loggedIn = false;
  document.body.classList.add('login-active');
  $('login-screen').hidden = false;
}

async function loadLiveHistory() {
  try {
    const hist = await getJson('/api/live/history');
    if (!Array.isArray(hist) || !hist.length) return;
    const cutoff = Date.now() / 1000 - LIVE_POINTS;
    live.ts = []; live.down = []; live.up = [];
    hist.forEach((p) => {
      if (p.ts < cutoff) return;
      live.ts.push(p.ts); live.down.push(p.down || 0); live.up.push(p.up || 0);
    });
    if (!$('view-home').hidden) drawLive();
  } catch { /* server history unavailable -- keep whatever localStorage gave us */ }
}

function startApp() {
  loggedIn = true;
  document.body.classList.remove('login-active');
  $('login-screen').hidden = true;

  const savedView = localStorage.getItem('wifiapp-view');
  if (VIEWS.includes(savedView) && savedView !== 'home') show(savedView);
  else if (live.down.length) drawLive();  // show restored history immediately

  loadLiveHistory();
  tick();
  tickLive();
}

async function boot() {
  try {
    const session = await getJson('/api/session');
    if (session.authenticated) startApp();
    else showLogin();
  } catch (err) {
    showLogin();
  }
}

$('login-same').addEventListener('change', (e) => {
  $('login-router-row').hidden = e.target.checked;
  $('login-sub').textContent = e.target.checked
    ? 'Use the same admin passwords as the outdoor unit and router.'
    : 'Enter the admin passwords for the outdoor unit and router separately.';
});

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const oduPassword = $('login-odu-password').value;
  const routerPassword = $('login-same').checked
    ? oduPassword : $('login-router-password').value;
  const error = $('login-error');
  error.textContent = '';
  const submitButton = e.target.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  try {
    await getJson('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ odu_password: oduPassword, router_password: routerPassword }),
    });
    $('login-odu-password').value = '';
    $('login-router-password').value = '';
    startApp();
  } catch (err) {
    error.textContent = err.field === 'odu' ? `Outdoor unit: ${err.message}`
      : err.field === 'router' ? `Router: ${err.message}`
        : err.message;
  } finally {
    submitButton.disabled = false;
  }
});

setInterval(tick, POLL_MS);
setInterval(tickLive, LIVE_MS);
boot();
