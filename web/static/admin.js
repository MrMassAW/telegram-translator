const API = '/api';

function $(id) {
  return document.getElementById(id);
}

async function api(path, opts = {}) {
  const o = {
    credentials: 'include',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  };
  if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
    o.body = JSON.stringify(opts.body);
  }
  const r = await fetch(API + path, o);
  return r;
}

function showLogin() {
  $('login-screen').classList.remove('hidden');
  $('dashboard').classList.add('hidden');
}

function showDashboard() {
  $('login-screen').classList.add('hidden');
  $('dashboard').classList.remove('hidden');
}

let eventSource = null;
let adjustTelegramId = null;

function stopStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function startStream() {
  stopStream();
  eventSource = new EventSource(API + '/admin/stream');
  eventSource.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.stats) renderTelemetry(data.stats);
      if (data.recent_logs) renderLiveFeed(data.recent_logs);
      $('header-status').textContent = 'LIVE · ' + new Date().toISOString();
    } catch (e) {
      console.error(e);
    }
  };
  eventSource.onerror = () => {
    $('header-status').textContent = 'STREAM RECONNECTING…';
  };
}

function bindTelemetryDrill(container) {
  if (!container) return;
  container.querySelectorAll('.stat-row.drillable').forEach((row) => {
    row.onclick = () => {
      const metric = row.getAttribute('data-metric');
      const st = row.getAttribute('data-status') || '';
      openTelemetryDrill(metric, st);
    };
  });
}

function renderTelemetry(s) {
  const el = $('stats-telemetry');
  if (!el) return;
  const rows = [
    ['Users', s.users_total, 'users_total'],
    ['Blocked', s.users_blocked, 'users_blocked'],
    ['Liability (¢)', s.liability_cents, 'liability'],
    ['Reserved (¢)', s.reserved_cents, 'reserved'],
    ['Revenue (¢)', s.revenue_usd_cents, 'revenue'],
    ['Translations 24h', s.translations_24h, 'translations_24h'],
  ];
  el.innerHTML = rows
    .map(
      ([k, v, metric]) =>
        `<div class="stat-row drillable" data-metric="${escapeHtml(metric)}" title="Click for details"><span>${escapeHtml(k)}</span><span class="stat-val">${escapeHtml(String(v))}</span></div>`
    )
    .join('');
  const st = s.translation_by_status || {};
  const extra = Object.entries(st)
    .map(
      ([k, v]) =>
        `<div class="stat-row drillable" data-metric="by_status" data-status="${escapeHtml(k)}" title="Click for details"><span>status:${escapeHtml(k)}</span><span class="stat-val">${escapeHtml(String(v))}</span></div>`
    )
    .join('');
  el.innerHTML += extra;
  bindTelemetryDrill(el);
}

/** All Telegram-related links open in a new tab/window. */
const TG_BLANK = ' target="_blank" rel="noopener noreferrer"';

function escapeAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/\r|\n/g, '');
}

function isAllowedTelegramHref(href) {
  const u = String(href).trim().toLowerCase();
  return u.startsWith('https://') || u.startsWith('http://') || u.startsWith('tg://');
}

/** Open a Telegram user (positive id) in the app. */
function tgUserUrl(id) {
  return 'tg://user?id=' + encodeURIComponent(String(id));
}

/**
 * Open a chat: private/small positive id → user; supergroup/channel (-100…) → t.me/c/…
 * See https://core.telegram.org/api/links
 */
function tgChatUrl(chatId) {
  const id = Number(chatId);
  if (Number.isNaN(id) || id === 0) return '#';
  if (id > 0) return tgUserUrl(id);
  const s = String(id);
  if (s.startsWith('-100')) {
    return 'https://t.me/c/' + encodeURIComponent(s.slice(4)) + '/1';
  }
  return 'https://t.me/c/' + encodeURIComponent(String(Math.abs(id))) + '/1';
}

function renderDrillCell(key, val, row) {
  if (val === null || val === undefined || val === '') return '—';
  const v = escapeHtml(String(val));

  if (key === 'source_link' || key === 'destination_link') {
    const u = String(val).trim();
    if (!u) return '—';
    if (!isAllowedTelegramHref(u)) return v;
    const label = key === 'source_link' ? 'Source message' : 'Destination message';
    return `<a class="tg-link" href="${escapeAttr(u)}"${TG_BLANK} title="Open exact message in Telegram">${escapeHtml(label)}</a>`;
  }

  if (key === 'owner_telegram_id' || key === 'telegram_id') {
    const num = Number(val);
    if (!Number.isNaN(num) && num !== 0) {
      return `<a class="tg-link" href="${escapeAttr(tgUserUrl(num))}"${TG_BLANK} title="Open user in Telegram">${v}</a>`;
    }
  }
  if (key === 'source_id' || key === 'destination_group_id') {
    const num = Number(val);
    if (!Number.isNaN(num) && num !== 0) {
      const href = tgChatUrl(num);
      return `<a class="tg-link" href="${escapeAttr(href)}"${TG_BLANK} title="Open chat in Telegram">${v}</a>`;
    }
  }
  if (key === 'source_name' && row.source_id != null && row.source_id !== '') {
    const num = Number(row.source_id);
    if (!Number.isNaN(num) && num !== 0) {
      return `<a class="tg-link" href="${escapeAttr(tgChatUrl(num))}"${TG_BLANK} title="Open chat in Telegram">${v}</a>`;
    }
  }
  if (key === 'dest_name' && row.destination_group_id != null && row.destination_group_id !== '') {
    const num = Number(row.destination_group_id);
    if (!Number.isNaN(num) && num !== 0) {
      return `<a class="tg-link" href="${escapeAttr(tgChatUrl(num))}"${TG_BLANK} title="Open chat in Telegram">${v}</a>`;
    }
  }
  if (key === 'username' && val && String(val).trim()) {
    const u = String(val).replace(/^@/, '').trim();
    if (/^[a-zA-Z][a-zA-Z0-9_]{3,31}$/.test(u)) {
      return `<a class="tg-link" href="https://t.me/${encodeURIComponent(u)}"${TG_BLANK} title="Open @${escapeHtml(u)}">${v}</a>`;
    }
  }
  return v;
}

function drillTableColumnOrder(firstRow) {
  const preferred = [
    'id',
    'owner_telegram_id',
    'status',
    'source_id',
    'source_name',
    'source_link',
    'destination_group_id',
    'dest_name',
    'destination_link',
    'error_message',
    'cost_usd_cents',
    'cost_usd',
    'created_at',
  ];
  const keys = Object.keys(firstRow);
  const out = [];
  for (const k of preferred) {
    if (keys.includes(k)) out.push(k);
  }
  for (const k of keys) {
    if (!out.includes(k)) out.push(k);
  }
  return out;
}

function renderDrillTable(rows) {
  if (!rows || rows.length === 0) return '<p style="color:var(--muted);font-size:12px;">No rows.</p>';
  const keys = drillTableColumnOrder(rows[0]);
  const thead = `<tr>${keys.map((k) => `<th>${escapeHtml(k)}</th>`).join('')}</tr>`;
  const tbody = rows
    .map((r) => `<tr>${keys.map((k) => `<td>${renderDrillCell(k, r[k], r)}</td>`).join('')}</tr>`)
    .join('');
  return `<table class="data"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
}

async function openTelemetryDrill(metric, statusVal) {
  const modal = $('modal-telemetry');
  const titleEl = $('telemetry-modal-title');
  const descEl = $('telemetry-modal-desc');
  const bodyEl = $('telemetry-modal-body');
  if (!modal || !titleEl || !bodyEl) return;
  titleEl.textContent = 'Loading…';
  descEl.textContent = '';
  bodyEl.innerHTML = '<p style="color:var(--muted);">Fetching…</p>';
  modal.classList.remove('hidden');

  let url = '/admin/telemetry/drill?metric=' + encodeURIComponent(metric) + '&limit=200';
  if (metric === 'by_status' && statusVal) {
    url += '&status=' + encodeURIComponent(statusVal);
  }
  const res = await api(url);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    titleEl.textContent = 'Error';
    descEl.textContent = '';
    bodyEl.innerHTML = `<p style="color:var(--danger);">${escapeHtml(err.detail || res.statusText)}</p>`;
    return;
  }
  const data = await res.json();
  titleEl.textContent = data.title || metric;
  descEl.textContent =
    (data.description || '') +
    (data.truncated ? ' · List truncated; increase limit in API if needed.' : '') +
    ' · Links open in a new tab. Use source_link / destination_link for exact messages when present.';
  bodyEl.innerHTML = renderDrillTable(data.rows);
}

async function loadFinancial() {
  const r = await api('/admin/financial');
  if (!r.ok) return;
  const f = await r.json();
  const el = $('stats-financial');
  if (!el) return;
  const rows = [
    ['Revenue (USD)', f.revenue_usd.toFixed(2), 'revenue'],
    ['User liability (USD)', f.user_liability_usd.toFixed(2), 'liability'],
    ['Manual provider balance (USD)', f.manual_provider_balance_usd != null ? f.manual_provider_balance_usd.toFixed(2) : '—', ''],
    ['Manual monthly provider cost (USD)', f.manual_monthly_provider_cost_usd != null ? f.manual_monthly_provider_cost_usd.toFixed(2) : '—', ''],
    ['Gross margin (USD, est.)', f.gross_margin_usd.toFixed(2), ''],
  ];
  el.innerHTML = rows
    .map(([k, v, metric]) => {
      if (metric) {
        return `<div class="stat-row drillable" data-metric="${escapeHtml(metric)}" title="Click for details"><span>${escapeHtml(k)}</span><span class="stat-val">${escapeHtml(String(v))}</span></div>`;
      }
      return `<div class="stat-row"><span>${escapeHtml(k)}</span><span class="stat-val">${escapeHtml(String(v))}</span></div>`;
    })
    .join('');
  bindTelemetryDrill(el);
}

function renderLiveFeed(logs) {
  const el = $('live-feed');
  if (!el) return;
  el.innerHTML = (logs || [])
    .slice(0, 40)
    .map((l) => {
      const st = (l.status || '').toLowerCase();
      const cls = st === 'success' ? 'ok' : 'fail';
      const cost = l.cost_usd_cents != null ? (l.cost_usd_cents / 100).toFixed(2) : '—';
      const oid = l.owner_telegram_id;
      const ownerCell =
        oid != null && oid !== ''
          ? `<a class="tg-link" href="${escapeAttr(tgUserUrl(Number(oid)))}"${TG_BLANK} title="Open user in Telegram">${escapeHtml(String(oid))}</a>`
          : '—';
      const sn = l.source_name || '';
      const dn = l.dest_name || '';
      const srcLink =
        l.source_id != null && l.source_id !== ''
          ? `<a class="tg-link" href="${escapeAttr(tgChatUrl(Number(l.source_id)))}"${TG_BLANK}>${escapeHtml(sn || String(l.source_id))}</a>`
          : escapeHtml(sn);
      const dstLink =
        l.destination_group_id != null && l.destination_group_id !== ''
          ? `<a class="tg-link" href="${escapeAttr(tgChatUrl(Number(l.destination_group_id)))}"${TG_BLANK}>${escapeHtml(dn || String(l.destination_group_id))}</a>`
          : escapeHtml(dn);
      const line = `${srcLink} → ${dstLink}`;
      const sl = l.source_link && String(l.source_link).trim() && isAllowedTelegramHref(l.source_link);
      const dl = l.destination_link && String(l.destination_link).trim() && isAllowedTelegramHref(l.destination_link);
      const msgLinks =
        sl || dl
          ? `<div style="margin-top:0.35rem;display:flex;flex-wrap:wrap;gap:0.5rem;">
            ${sl ? `<a class="tg-link" href="${escapeAttr(l.source_link.trim())}"${TG_BLANK}>Source message</a>` : ''}
            ${dl ? `<a class="tg-link" href="${escapeAttr(l.destination_link.trim())}"${TG_BLANK}>Destination message</a>` : ''}
            </div>`
          : '';
      const err = l.error_message ? ' · ' + escapeHtml(l.error_message) : '';
      return `<div class="row"><span class="${cls}">${escapeHtml(st)}</span><span>${ownerCell}</span><span>$${cost}</span></div>
        <div style="font-size:10px;color:var(--muted);margin:-0.2rem 0 0.4rem;padding-left:0.2rem;">${line}${err}</div>${msgLinks}`;
    })
    .join('');
}

function escapeHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function loadClients() {
  const q = ($('client-q') && $('client-q').value) || '';
  const r = await api('/admin/clients?q=' + encodeURIComponent(q) + '&limit=100');
  if (!r.ok) return;
  const data = await r.json();
  const tb = $('clients-tbody');
  if (!tb) return;
  tb.innerHTML = (data.clients || [])
    .map((c) => {
      const blocked = c.blocked ? 'YES' : 'no';
      const fc = c.free_balance_cents != null ? c.free_balance_cents : 0;
      const pc = c.paid_balance_cents != null ? c.paid_balance_cents : 0;
      const tot = c.balance != null ? c.balance : fc + pc;
      return `<tr>
        <td>${c.telegram_id}</td>
        <td>${escapeHtml(c.username || '—')}</td>
        <td>$${(fc / 100).toFixed(2)}</td>
        <td>$${(pc / 100).toFixed(2)}</td>
        <td>$${(tot / 100).toFixed(2)}</td>
        <td>${c.reserved_balance}</td>
        <td>${c.rule_count}</td>
        <td>${blocked}</td>
        <td>
          <button type="button" class="primary" data-act="adj" data-id="${c.telegram_id}" title="Adjust free credit only (Stripe adds paid)">Free credit</button>
          <button type="button" class="${c.blocked ? 'primary' : 'danger'}" data-act="block" data-id="${c.telegram_id}" data-blocked="${c.blocked}">${c.blocked ? 'Unblock' : 'Block'}</button>
        </td>
      </tr>`;
    })
    .join('');
  tb.querySelectorAll('button[data-act]').forEach((btn) => {
    btn.onclick = () => {
      const id = parseInt(btn.getAttribute('data-id'), 10);
      const act = btn.getAttribute('data-act');
      if (act === 'adj') openAdjust(id);
      if (act === 'block') toggleBlock(id, btn.getAttribute('data-blocked') === 'true');
    };
  });
}

async function toggleBlock(telegramId, currentlyBlocked) {
  const r = await api('/admin/clients/' + telegramId, {
    method: 'PATCH',
    body: { blocked: !currentlyBlocked },
  });
  if (r.ok) await loadClients();
  else alert((await r.json().catch(() => ({}))).detail || 'Failed');
}

function openAdjust(telegramId) {
  adjustTelegramId = telegramId;
  $('modal-adjust-user').textContent = 'telegram_id: ' + telegramId;
  $('adj-usd').value = '1.00';
  $('adj-reason').value = 'bonus';
  $('adj-note').value = '';
  $('modal-adjust').classList.remove('hidden');
}

function closeAdjust() {
  $('modal-adjust').classList.add('hidden');
  adjustTelegramId = null;
}

async function submitAdjust() {
  if (adjustTelegramId == null) return;
  const usd = parseFloat(String($('adj-usd').value || '').replace(',', '.'));
  const deltaCents = Math.round(usd * 100);
  const reason = ($('adj-reason').value || '').trim();
  const note = ($('adj-note').value || '').trim() || null;
  if (isNaN(usd) || deltaCents === 0) {
    alert('Enter a non-zero amount in USD (adds or removes free credit; paid is Stripe only)');
    return;
  }
  if (!reason) {
    alert('Enter reason');
    return;
  }
  const r = await api('/admin/credits/adjust', {
    method: 'POST',
    body: { telegram_id: adjustTelegramId, delta_cents: deltaCents, reason, note },
  });
  if (r.ok) {
    closeAdjust();
    await loadClients();
    await loadFinancial();
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed');
  }
}

async function loadWhitelist() {
  const r = await api('/admin/whitelist');
  if (!r.ok) return;
  const rows = await r.json();
  const box = $('whitelist-box');
  if (!box) return;
  box.innerHTML = rows
    .map(
      (u) =>
        `<div><span>${u.telegram_id} ${escapeHtml(u.username || '')}</span><button type="button" data-wlid="${u.id}">Remove</button></div>`
    )
    .join('');
  box.querySelectorAll('button[data-wlid]').forEach((b) => {
    b.onclick = async () => {
      const id = parseInt(b.getAttribute('data-wlid'), 10);
      const x = await api('/admin/whitelist/' + id, { method: 'DELETE' });
      if (x.ok) await loadWhitelist();
    };
  });
}

async function loadSettingsFields() {
  const r = await api('/admin/settings');
  if (!r.ok) return;
  const s = await r.json();
  $('set-max-pairs').value = s.max_pairs;
  $('set-max-dest').value = s.max_destinations_per_source;
  $('set-max-len').value = s.max_message_length;
  const pct = s.cents_per_text_translation != null ? s.cents_per_text_translation / 100 : 0.01;
  const pci = s.cents_per_image_translation != null ? s.cents_per_image_translation / 100 : 0.1;
  $('set-price-text').value = pct.toFixed(2);
  $('set-price-image').value = pci.toFixed(2);
  $('set-provider-bal').value =
    s.manual_provider_balance_cents != null ? (s.manual_provider_balance_cents / 100).toFixed(2) : '';
  $('set-provider-cost').value =
    s.manual_monthly_provider_cost_cents != null ? (s.manual_monthly_provider_cost_cents / 100).toFixed(2) : '';
}

async function saveSettings() {
  const max_pairs = parseInt($('set-max-pairs').value, 10);
  const max_destinations_per_source = parseInt($('set-max-dest').value, 10);
  const max_message_length = parseInt($('set-max-len').value, 10);
  const pt = parseFloat($('set-price-text').value);
  const pi = parseFloat($('set-price-image').value);
  const pb = parseFloat($('set-provider-bal').value);
  const pc = parseFloat($('set-provider-cost').value);
  const body = {
    max_pairs,
    max_destinations_per_source,
    max_message_length,
    cents_per_text_translation: isNaN(pt) ? 0 : Math.round(pt * 100),
    cents_per_image_translation: isNaN(pi) ? 0 : Math.round(pi * 100),
    manual_provider_balance_cents: isNaN(pb) ? null : Math.round(pb * 100),
    manual_monthly_provider_cost_cents: isNaN(pc) ? null : Math.round(pc * 100),
  };
  const r = await api('/admin/settings', { method: 'PATCH', body });
  if (r.ok) {
    await loadFinancial();
    alert('Saved');
  } else alert((await r.json().catch(() => ({}))).detail || 'Failed');
}

function tickClock() {
  const el = $('clock-line');
  if (el) el.textContent = new Date().toISOString();
}

async function checkSession() {
  const r = await api('/admin/me');
  if (r.ok) {
    showDashboard();
    tickClock();
    setInterval(tickClock, 1000);
    await loadSettingsFields();
    await loadFinancial();
    await loadClients();
    await loadWhitelist();
    const st = await api('/admin/stats');
    if (st.ok) renderTelemetry(await st.json());
    startStream();
  } else {
    showLogin();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('login-form').onsubmit = async (e) => {
    e.preventDefault();
    $('login-err').textContent = '';
    const password = $('login-password').value;
    const r = await api('/admin/login', { method: 'POST', body: { password } });
    if (r.ok) {
      $('login-password').value = '';
      await checkSession();
    } else {
      const d = await r.json().catch(() => ({}));
      $('login-err').textContent = d.detail || 'Login failed';
    }
  };

  $('btn-logout').onclick = async () => {
    stopStream();
    await api('/admin/logout', { method: 'POST' });
    showLogin();
  };

  $('btn-clients-refresh').onclick = () => loadClients();
  $('client-q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') loadClients();
  });

  $('btn-save-settings').onclick = () => saveSettings();

  $('btn-wl-add').onclick = async () => {
    const telegram_id = parseInt($('wl-tid').value, 10);
    const username = ($('wl-user').value || '').trim() || null;
    if (isNaN(telegram_id)) {
      alert('Enter numeric telegram id');
      return;
    }
    const r = await api('/admin/whitelist', { method: 'POST', body: { telegram_id, username } });
    if (r.ok) {
      $('wl-tid').value = '';
      $('wl-user').value = '';
      await loadWhitelist();
    } else alert((await r.json().catch(() => ({}))).detail || 'Failed');
  };

  $('adj-cancel').onclick = closeAdjust;
  $('adj-submit').onclick = submitAdjust;

  const telModal = $('modal-telemetry');
  const telClose = $('telemetry-modal-close');
  if (telClose && telModal) {
    telClose.onclick = () => {
      telModal.classList.add('hidden');
    };
    telModal.onclick = (e) => {
      if (e.target === telModal) telModal.classList.add('hidden');
    };
  }

  checkSession();
});
