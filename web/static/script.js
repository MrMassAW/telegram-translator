const tg = window.Telegram?.WebApp || {};
if (tg.expand) tg.expand();

function hasTelegramIdentity() {
  const d = tg.initData;
  return typeof d === 'string' && d.trim().length > 0;
}

/** Replace main UI when opened outside Telegram (no signed initData). */
function showTelegramRequiredGate() {
  const app = document.getElementById('app');
  if (!app) return;
  app.innerHTML = `
    <div class="min-h-screen flex flex-col items-center justify-center p-6 text-center bg-background-light dark:bg-background-dark text-slate-800 dark:text-slate-100">
      <div class="max-w-sm space-y-4">
        <div class="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-primary text-white">
          <span class="material-symbols-outlined text-3xl">lock</span>
        </div>
        <h1 class="text-lg font-bold">Open in Telegram</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">This mini app only works inside Telegram with your account. Open it from the bot&apos;s <strong class="text-slate-800 dark:text-slate-200">Open</strong> button or menu link.</p>
        <p class="text-xs text-slate-500 dark:text-slate-500">A regular browser cannot access your translation settings.</p>
      </div>
    </div>`;
}

const THEME_KEY = 'app-theme';
let systemThemeListener = null;

function applyTheme(theme) {
  const root = document.documentElement;
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  if (systemThemeListener) {
    media.removeEventListener('change', systemThemeListener);
    systemThemeListener = null;
  }
  if (theme === 'light') {
    root.classList.remove('dark');
  } else if (theme === 'dark') {
    root.classList.add('dark');
  } else {
    root.classList.toggle('dark', media.matches);
    systemThemeListener = () => root.classList.toggle('dark', window.matchMedia('(prefers-color-scheme: dark)').matches);
    media.addEventListener('change', systemThemeListener);
  }
  updateThemeButtons();
}

function updateThemeButtons() {
  const btns = document.querySelectorAll('.theme-btn');
  if (!btns.length) return;
  const current = localStorage.getItem(THEME_KEY) || 'system';
  btns.forEach(btn => {
    const isActive = btn.dataset.theme === current;
    btn.classList.toggle('bg-primary', isActive);
    btn.classList.toggle('text-white', isActive);
    btn.classList.toggle('text-slate-600', !isActive);
    btn.classList.toggle('dark:text-slate-300', !isActive);
  });
}

function initTheme() {
  const theme = localStorage.getItem(THEME_KEY) || 'system';
  applyTheme(theme);
}

initTheme();

const API_BASE = '/api';
let rules = [];
let sources = [];  // standalone sources (no destinations yet)
let chatsWithAccess = [];
let credits = 0;
let creditsFree = 0;
let creditsPaid = 0;
let settings = { receive_reports_telegram: true, spam_protection_enabled: false, spam_max_messages: 50, spam_window_minutes: 5 };
let selectedSource = null; // { id, title, access }
let editingRuleId = null;
let editingSourceId = null;
let collapsedSourceIds = new Set(JSON.parse(sessionStorage.getItem('collapsedSources') || '[]'));
let addingForSourceId = null;
let limits = { max_pairs: 10, max_destinations_per_source: 10, max_message_length: 4096, bot_username: '' };
const BROWSE_GROUPS_VALUE = '__browse__';
const SESSION_PAIRING_SOURCE = 'pairingSourceId';
const SESSION_PAIRING_RULE = 'pairingRuleId';
/** @type {Record<number, string[]>} */
let termsBySource = {};
let pendingExcludedTermsImportSourceId = null;
const NO_ACCESS_TOOLTIP = 'Bot does not have access to this chat. Add the bot as administrator (or re-add the bot) and refresh the list.';
const ACTION_LOG_KEY = 'teletranslate-ui-action-log-v1';
const ACTION_LOG_MAX = 120;
let actionLog = [];
let toastHideTimer = null;

function loadActionLog() {
  try {
    const raw = localStorage.getItem(ACTION_LOG_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(entry => entry && typeof entry.message === 'string')
      .map(entry => ({
        message: String(entry.message),
        type: entry.type === 'success' || entry.type === 'error' || entry.type === 'warning' ? entry.type : 'info',
        createdAt: entry.createdAt || new Date().toISOString(),
      }))
      .slice(-ACTION_LOG_MAX);
  } catch (e) {
    return [];
  }
}

function persistActionLog() {
  try {
    localStorage.setItem(ACTION_LOG_KEY, JSON.stringify(actionLog.slice(-ACTION_LOG_MAX)));
  } catch (e) {}
}

function ensureActionUi() {
  if (document.getElementById('action-toast')) return;
  const host = document.getElementById('app') || document.body;
  if (!host) return;
  const html = `
    <div id="action-toast" class="hidden fixed bottom-24 left-3 right-3 z-[70] sm:left-auto sm:right-4 sm:w-[360px] rounded-xl border border-slate-200 dark:border-slate-700 bg-white/95 dark:bg-slate-900/95 shadow-xl backdrop-blur p-3 cursor-pointer transition-opacity">
      <div class="flex items-start gap-2">
        <span id="action-toast-icon" class="material-symbols-outlined text-primary text-lg leading-none mt-[2px]">info</span>
        <div class="min-w-0 flex-1">
          <p id="action-toast-message" class="text-sm font-medium text-slate-800 dark:text-slate-100 truncate"></p>
          <p class="text-[10px] text-slate-500 dark:text-slate-400 mt-1">Tap to open activity history</p>
        </div>
      </div>
    </div>
    <div id="action-log-modal" class="hidden fixed inset-0 z-[80] bg-black/50 p-4" aria-hidden="true">
      <div class="mx-auto mt-8 max-w-lg rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-2xl">
        <div class="flex items-center justify-between p-4 border-b border-slate-200 dark:border-slate-800">
          <h3 class="text-base font-bold">Recent activity</h3>
          <button type="button" id="action-log-close" class="text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
            <span class="material-symbols-outlined">close</span>
          </button>
        </div>
        <div id="action-log-list" class="max-h-[60vh] overflow-y-auto p-4 space-y-2"></div>
        <div class="p-4 border-t border-slate-200 dark:border-slate-800">
          <button type="button" id="action-log-clear" class="w-full py-2 rounded-lg border border-slate-300 dark:border-slate-600 text-sm font-semibold hover:bg-slate-50 dark:hover:bg-slate-800">Clear activity</button>
        </div>
      </div>
    </div>
  `;
  host.insertAdjacentHTML('beforeend', html);
  const toast = document.getElementById('action-toast');
  const modal = document.getElementById('action-log-modal');
  const closeBtn = document.getElementById('action-log-close');
  const clearBtn = document.getElementById('action-log-clear');
  if (toast) {
    toast.addEventListener('click', () => openActionLogModal());
  }
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeActionLogModal();
    });
  }
  if (closeBtn) closeBtn.addEventListener('click', closeActionLogModal);
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      actionLog = [];
      persistActionLog();
      renderActionLogList();
      showToastMessage('Activity log cleared', 'info');
    });
  }
}

function renderActionLogList() {
  const list = document.getElementById('action-log-list');
  if (!list) return;
  if (!actionLog.length) {
    list.innerHTML = '<p class="text-sm text-slate-500 dark:text-slate-400">No activity yet.</p>';
    return;
  }
  list.innerHTML = actionLog.slice().reverse().map(entry => {
    const color = entry.type === 'error'
      ? 'text-red-500'
      : (entry.type === 'success' ? 'text-emerald-500' : (entry.type === 'warning' ? 'text-amber-500' : 'text-primary'));
    const icon = entry.type === 'error'
      ? 'error'
      : (entry.type === 'success' ? 'check_circle' : (entry.type === 'warning' ? 'warning' : 'info'));
    const time = new Date(entry.createdAt).toLocaleString();
    return `
      <div class="rounded-lg border border-slate-200 dark:border-slate-700 p-3 bg-slate-50 dark:bg-slate-800/40">
        <div class="flex items-start gap-2">
          <span class="material-symbols-outlined text-base ${color}">${icon}</span>
          <div class="min-w-0">
            <p class="text-sm text-slate-800 dark:text-slate-100 break-words">${escapeHtml(entry.message)}</p>
            <p class="text-[10px] text-slate-500 dark:text-slate-400 mt-1">${escapeHtml(time)}</p>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function openActionLogModal() {
  const modal = document.getElementById('action-log-modal');
  if (!modal) return;
  renderActionLogList();
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
}

function closeActionLogModal() {
  const modal = document.getElementById('action-log-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
}

function showToastMessage(message, type = 'info') {
  const toast = document.getElementById('action-toast');
  const msg = document.getElementById('action-toast-message');
  const icon = document.getElementById('action-toast-icon');
  if (!toast || !msg || !icon) return;
  msg.textContent = message;
  icon.textContent = type === 'error'
    ? 'error'
    : (type === 'success' ? 'check_circle' : (type === 'warning' ? 'warning' : 'info'));
  icon.className = 'material-symbols-outlined text-lg leading-none mt-[2px] ' + (
    type === 'error'
      ? 'text-red-500'
      : (type === 'success' ? 'text-emerald-500' : (type === 'warning' ? 'text-amber-500' : 'text-primary'))
  );
  toast.classList.remove('hidden');
  toast.classList.remove('opacity-0');
  if (toastHideTimer) clearTimeout(toastHideTimer);
  toastHideTimer = setTimeout(() => {
    toast.classList.add('opacity-0');
    setTimeout(() => toast.classList.add('hidden'), 160);
  }, 3500);
}

function logAction(message, type = 'info', showToast = true) {
  actionLog.push({ message, type, createdAt: new Date().toISOString() });
  if (actionLog.length > ACTION_LOG_MAX) actionLog = actionLog.slice(-ACTION_LOG_MAX);
  persistActionLog();
  if (showToast) showToastMessage(message, type);
}

function apiHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (tg.initData) h['X-Telegram-Init-Data'] = tg.initData;
  return h;
}

function showView(name) {
  document.querySelectorAll('.view').forEach(el => el.classList.add('hidden'));
  const view = document.getElementById('view-' + name);
  if (view) view.classList.remove('hidden');
  if (name === 'add-source') {
    window.scrollTo(0, 0);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }
  document.querySelectorAll('.nav-btn').forEach(btn => {
    const isActive = btn.dataset.view === name;
    btn.className = btn.className.replace(/text-primary|text-slate-400|text-slate-500/g, '').trim();
    btn.classList.add(isActive ? 'text-primary' : 'text-slate-400', 'dark:text-slate-500');
  });
  if (name === 'account') updateThemeButtons();
}

async function init() {
  try {
    if (!hasTelegramIdentity()) {
      showTelegramRequiredGate();
      return;
    }
    if (tg.ready) tg.ready();
    try {
      const res = await fetch(API_BASE + '/validate-init-data', { method: 'POST', headers: apiHeaders(), body: JSON.stringify({ initData: tg.initData }) });
      if (!res.ok) {
        document.body.innerHTML = '<div class="min-h-screen flex items-center justify-center p-8 bg-background-light dark:bg-background-dark"><h1 class="text-lg font-semibold text-slate-800 dark:text-slate-100">Unauthorized</h1></div>';
        return;
      }
    } catch (e) {
      document.body.innerHTML = '<div class="min-h-screen flex items-center justify-center p-8 bg-background-light dark:bg-background-dark"><h1 class="text-lg font-semibold text-slate-800 dark:text-slate-100">Error connecting to server</h1></div>';
      return;
    }
    await Promise.all([loadRules(), loadSources(), loadExcludedTerms(), loadChatsWithAccess(), loadCredits(), loadSettings(), loadLimits()]);
    bindEvents();
    renderDashboard();
    consumeBillingQueryParams();
  } catch (e) {
    console.error('Init error:', e);
    const container = document.getElementById('pair-cards');
    if (container) container.innerHTML = '<p class="p-4 text-red-500">Something went wrong. Try refreshing.</p>';
  }
}

async function loadLimits() {
  try {
    const r = await fetch(API_BASE + '/settings/limits', { headers: apiHeaders() });
    if (r.ok) {
      const d = await r.json();
      limits = { ...limits, ...d };
    }
  } catch (e) {}
}

function openBrowseGroupsForDest(sourceId, ruleId) {
  const un = limits.bot_username;
  if (!un) {
    alert('Could not load the bot link. Refresh the app and try again.');
    return;
  }
  if (sourceId != null) sessionStorage.setItem(SESSION_PAIRING_SOURCE, String(sourceId));
  else sessionStorage.removeItem(SESSION_PAIRING_SOURCE);
  if (ruleId != null) sessionStorage.setItem(SESSION_PAIRING_RULE, String(ruleId));
  else sessionStorage.removeItem(SESSION_PAIRING_RULE);
  const url = 'https://t.me/' + un + '?start=pickdest';
  if (tg.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, '_blank', 'noopener,noreferrer');
}

async function loadRules() {
  try {
    const r = await fetch(API_BASE + '/rules', { headers: apiHeaders() });
    const data = await r.json();
    rules = r.ok && Array.isArray(data) ? data : [];
  } catch (e) { rules = []; }
}

async function loadSources() {
  try {
    const r = await fetch(API_BASE + '/sources', { headers: apiHeaders() });
    const data = await r.json();
    sources = r.ok && Array.isArray(data) ? data : [];
  } catch (e) { sources = []; }
}

async function loadExcludedTerms() {
  if (!tg.initData) return;
  try {
    const r = await fetch(API_BASE + '/excluded-terms', { headers: apiHeaders() });
    if (!r.ok) { termsBySource = {}; return; }
    const d = await r.json();
    termsBySource = {};
    const raw = d.terms_by_source || {};
    for (const k of Object.keys(raw)) {
      const sid = parseInt(k, 10);
      if (Number.isNaN(sid)) continue;
      const arr = raw[k];
      termsBySource[sid] = Array.isArray(arr) ? arr.filter(x => typeof x === 'string') : [];
    }
  } catch (e) {
    termsBySource = {};
  }
}

function parseCommaSeparatedTerms(s) {
  if (s == null || !String(s).trim()) return [];
  return String(s).split(',').map(t => t.trim()).filter(Boolean);
}

async function loadChatsWithAccess() {
  try {
    const r = await fetch(API_BASE + '/chats/with-access', { headers: apiHeaders() });
    const data = r.ok ? await r.json() : [];
    chatsWithAccess = Array.isArray(data) ? data : [];
  } catch (e) { chatsWithAccess = []; }
}

async function loadCredits() {
  if (!tg.initData) return;
  try {
    const r = await fetch(API_BASE + '/credits', { headers: apiHeaders() });
    if (r.ok) {
      const d = await r.json();
      credits = d.balance;
      creditsFree = d.free_balance_cents != null ? d.free_balance_cents : 0;
      creditsPaid = d.paid_balance_cents != null ? d.paid_balance_cents : 0;
    }
  } catch (e) {}
}

function updateBalanceDisplay() {
  const s = (Number(credits) / 100).toFixed(2) + ' USD';
  const headerEl = document.getElementById('header-credits');
  if (headerEl) headerEl.textContent = s;
  const accountEl = document.getElementById('account-credits');
  if (accountEl) accountEl.textContent = s;
  const br = document.getElementById('account-credits-breakdown');
  if (br) {
    const f = (Number(creditsFree) / 100).toFixed(2);
    const p = (Number(creditsPaid) / 100).toFixed(2);
    br.textContent = `Free ${f} USD · Paid ${p} USD (free is used first)`;
  }
}

function apiErrorDetail(d) {
  if (!d || d.detail == null) return 'Request failed';
  if (typeof d.detail === 'string') return d.detail;
  if (Array.isArray(d.detail)) return d.detail.map((x) => (x && x.msg) || JSON.stringify(x)).join(', ');
  return String(d.detail);
}

async function loadBillingLedger() {
  const list = document.getElementById('billing-ledger-list');
  if (!list || !tg.initData) return;
  try {
    const r = await fetch(API_BASE + '/billing/ledger?limit=10', { headers: apiHeaders() });
    if (!r.ok) {
      list.innerHTML = '<li class="text-slate-500">Could not load activity.</li>';
      return;
    }
    const rows = await r.json();
    if (!Array.isArray(rows) || !rows.length) {
      list.innerHTML = '<li class="text-slate-500">No purchases yet.</li>';
      return;
    }
    list.innerHTML = rows.map((row) => {
      const amt = (Number(row.delta_cents) / 100).toFixed(2);
      const sign = Number(row.delta_cents) >= 0 ? '+' : '';
      const src = row.source === 'stripe' ? 'Stripe' : String(row.source || '');
      return `<li><span class="font-medium tabular-nums">${sign}$${amt}</span> <span class="text-slate-400">(${src})</span></li>`;
    }).join('');
  } catch (e) {
    list.innerHTML = '<li class="text-slate-500">Could not load activity.</li>';
  }
}

function consumeBillingQueryParams() {
  try {
    const u = new URL(window.location.href);
    const b = u.searchParams.get('billing');
    if (b === 'success') {
      loadCredits().then(() => {
        updateBalanceDisplay();
        loadBillingLedger();
        logAction('Billing: payment completed. Balance refreshed.', 'success', true);
      });
      u.searchParams.delete('billing');
      const q = u.searchParams.toString();
      window.history.replaceState({}, '', u.pathname + (q ? '?' + q : '') + u.hash);
    } else if (b === 'cancel') {
      logAction('Billing: checkout cancelled.', 'info', true);
      u.searchParams.delete('billing');
      const q = u.searchParams.toString();
      window.history.replaceState({}, '', u.pathname + (q ? '?' + q : '') + u.hash);
    }
  } catch (e) {}
}

async function startBillingCheckout(priceKey) {
  if (!tg.initData) return;
  try {
    const r = await fetch(API_BASE + '/billing/checkout-session', {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify({ price_key: priceKey }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert(apiErrorDetail(d));
      return;
    }
    if (d.url && tg.openLink) tg.openLink(d.url);
    else if (d.url) window.open(d.url, '_blank', 'noopener,noreferrer');
    else alert('No checkout URL returned');
  } catch (e) {
    alert('Could not start checkout');
  }
}

async function loadSettings() {
  if (!tg.initData) return;
  try {
    const r = await fetch(API_BASE + '/me/settings', { headers: apiHeaders() });
    if (r.ok) settings = await r.json();
  } catch (e) {}
}

function nameFor(id) {
  const c = chatsWithAccess.find(ch => ch.id === id);
  return c ? (c.title || String(id)) : String(id);
}

function hasAccess(chatId) {
  const c = chatsWithAccess.find(ch => ch.id === chatId);
  return c ? (c.access === true) : false;
}

function renderDashboard() {
  const rulesList = Array.isArray(rules) ? rules : [];
  const sourcesList = Array.isArray(sources) ? sources : [];
  const activeCount = rulesList.filter(r => r.enabled !== false).length;
  document.getElementById('badge-running').textContent = activeCount + ' Running';
  const headerCreditsEl = document.getElementById('header-credits');
  if (headerCreditsEl) headerCreditsEl.textContent = (Number(credits) / 100).toFixed(2) + ' USD';

  const container = document.getElementById('pair-cards');
  if (!container) return;
  container.innerHTML = '';

  const groupsBySource = new Map();
  for (const rule of rulesList) {
    const sid = rule.source_id;
    if (!groupsBySource.has(sid)) {
      groupsBySource.set(sid, { sourceId: sid, sourceName: nameFor(sid), rules: [] });
    }
    groupsBySource.get(sid).rules.push(rule);
  }
  for (const s of sourcesList) {
    const sid = s.source_id;
    if (!groupsBySource.has(sid)) {
      groupsBySource.set(sid, { sourceId: sid, sourceName: (s.title && s.title.trim()) ? s.title : nameFor(sid), rules: [] });
    }
  }

  const maxDest = limits.max_destinations_per_source ?? 10;
  const langOpts = ['none','en','es','fr','de','it','pt','ru','zh','ja','ko','uk','tr','ar','hi'].map(l => `<option value="${l}">${l === 'none' ? 'No translation' : l}</option>`).join('');
  const groupsList = chatsWithAccess.filter(c => c.type === 'group' || c.type === 'supergroup');

  for (const group of groupsBySource.values()) {
    const sourceAccess = hasAccess(group.sourceId);
    const sourceLabelClass = sourceAccess ? 'text-green-500 bg-green-500/10' : 'text-red-500 bg-red-500/10';
    const sourceEnabled = group.rules.some(r => r.enabled !== false);
    const isCollapsed = collapsedSourceIds.has(group.sourceId);
    const canAddDest = group.rules.length < maxDest;
    const hasRules = group.rules.length > 0;
    const card = document.createElement('div');
    card.className = 'rounded-lg bg-white dark:bg-slate-900 p-2 border border-slate-100 dark:border-slate-800 shadow-sm';
    const sourceAccessIcon = !sourceAccess ? `<span class="material-symbols-outlined text-red-500 text-sm shrink-0" title="${escapeHtml(NO_ACCESS_TOOLTIP)}">error</span>` : '';
    const sourceToggleHtml = hasRules
      ? `<label class="relative flex h-6 w-10 cursor-pointer items-center rounded-full bg-slate-200 dark:bg-slate-700 p-0.5 transition-colors has-[:checked]:bg-primary">
            <input type="checkbox" class="peer invisible absolute source-toggle" data-source-id="${group.sourceId}" ${sourceEnabled ? 'checked' : ''}/>
            <div class="h-5 w-5 rounded-full bg-white shadow transition-all peer-checked:translate-x-[16px]"></div>
          </label>`
      : '';
    const sourceRow = `
      <div class="flex items-center justify-between gap-1.5">
        <div class="flex items-center gap-1.5 flex-1 min-w-0">
          <button type="button" class="source-collapse shrink-0 p-0.5 rounded hover:bg-slate-100 dark:hover:bg-slate-800" data-source-id="${group.sourceId}" aria-label="${isCollapsed ? 'Expand' : 'Collapse'}">
            <span class="material-symbols-outlined text-slate-500 text-base">${isCollapsed ? 'expand_more' : 'expand_less'}</span>
          </button>
          <span class="text-[10px] font-bold px-1.5 py-0.5 rounded shrink-0 ${sourceLabelClass}" ${!sourceAccess ? `title="${escapeHtml(NO_ACCESS_TOOLTIP)}"` : ''}>SOURCE</span>
          ${sourceAccessIcon}
          <p class="text-xs font-semibold truncate">${escapeHtml(group.sourceName)}</p>
        </div>
        <div class="flex items-center gap-1 shrink-0">
          ${sourceToggleHtml}
          <button type="button" class="source-edit p-1 rounded-full hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500" data-source-id="${group.sourceId}" aria-label="Edit source"><span class="material-symbols-outlined text-base">edit</span></button>
          <button type="button" class="source-delete p-1 rounded-full hover:bg-red-500/10 text-slate-500 hover:text-red-500" data-source-id="${group.sourceId}" aria-label="Delete source"><span class="material-symbols-outlined text-base">delete</span></button>
        </div>
      </div>
    `;
    const destCount = group.rules.length;
    const arrowRow = `<div class="flex pl-1 py-0.5"><span class="material-symbols-outlined text-slate-400 text-xs">arrow_downward</span></div>`;
    const destRows = group.rules.map(rule => {
      const destAccess = hasAccess(rule.destination_group_id);
      const destLabelClass = destAccess ? 'text-green-500 bg-green-500/10' : 'text-red-500 bg-red-500/10';
      const enabled = rule.enabled !== false;
      const destAccessIcon = !destAccess ? `<span class="material-symbols-outlined text-red-500 text-xs shrink-0" title="${escapeHtml(NO_ACCESS_TOOLTIP)}">error</span>` : '';
      return `
        <div class="flex items-center justify-between gap-1.5 pl-1.5 border-l-2 border-slate-200 dark:border-slate-700 ml-0.5 py-1">
          <div class="flex items-center gap-1.5 flex-1 min-w-0">
            <span class="text-[10px] font-bold px-1.5 py-0.5 rounded shrink-0 ${destLabelClass}" ${!destAccess ? `title="${escapeHtml(NO_ACCESS_TOOLTIP)}"` : ''}>DEST</span>
            ${destAccessIcon}
            <p class="text-xs font-semibold truncate">${escapeHtml(nameFor(rule.destination_group_id))} (${rule.destination_language})</p>
          </div>
          <div class="flex items-center gap-1 shrink-0">
            <label class="relative flex h-6 w-10 cursor-pointer items-center rounded-full bg-slate-200 dark:bg-slate-700 p-0.5 transition-colors has-[:checked]:bg-primary">
              <input type="checkbox" class="peer invisible absolute rule-toggle" data-rule-id="${rule.id}" ${enabled ? 'checked' : ''}/>
              <div class="h-5 w-5 rounded-full bg-white shadow transition-all peer-checked:translate-x-[16px]"></div>
            </label>
            <button type="button" class="dest-edit p-1 rounded-full hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500" data-rule-id="${rule.id}" aria-label="Edit"><span class="material-symbols-outlined text-base">edit</span></button>
            <button type="button" class="dest-delete p-1 rounded-full hover:bg-red-500/10 text-slate-500 hover:text-red-500" data-rule-id="${rule.id}" aria-label="Delete"><span class="material-symbols-outlined text-base">delete</span></button>
          </div>
        </div>
      `;
    }).join('');
    const addDestRow = canAddDest
      ? (addingForSourceId === group.sourceId
          ? `
        <div class="pl-1.5 border-l-2 border-slate-200 dark:border-slate-700 ml-0.5 py-1.5 space-y-1.5 add-dest-form" data-source-id="${group.sourceId}">
          <select class="add-dest-select block w-full rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-xs h-8 px-2" data-source-id="${group.sourceId}">
            <option value="">Choose group...</option>
            <option value="${BROWSE_GROUPS_VALUE}">Browse groups…</option>
            ${groupsList.map(c => `<option value="${c.id}">${escapeHtml(c.title || c.id)} ${c.access ? '✓' : '✗'}</option>`).join('')}
          </select>
          <label class="flex items-start gap-2 p-2 bg-red-50 dark:bg-red-950/40 rounded-xl border border-red-200 dark:border-red-900/80 cursor-pointer">
            <input type="checkbox" class="add-dest-images mt-0.5 rounded border-red-300 dark:border-red-700 shrink-0" data-source-id="${group.sourceId}"/>
            <div class="flex flex-col gap-0.5 min-w-0">
              <span class="text-xs font-semibold text-red-900 dark:text-red-100">Translate text in images</span>
              <span class="text-[10px] text-red-800/90 dark:text-red-200/90 leading-snug">OCR and translate text inside photos. Each translated image costs 10¢. Experimental: translation aims to leave street names, road signs, and similar text unchanged when appropriate.</span>
            </div>
          </label>
          <div class="flex gap-1.5 items-center flex-wrap">
            <select class="add-dest-lang rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-xs h-8 px-2 flex-1 min-w-[120px]" data-source-id="${group.sourceId}">${langOpts}</select>
            <label class="flex items-center gap-1 text-xs shrink-0" title="Translate poll (new independent poll)"><input type="checkbox" class="add-dest-poll rounded" data-source-id="${group.sourceId}"/> Poll</label>
            <button type="button" class="add-dest-save px-2 py-1 rounded bg-primary text-white text-xs font-semibold" data-source-id="${group.sourceId}">Save</button>
            <button type="button" class="add-dest-cancel px-2 py-1 rounded border border-slate-300 text-xs" data-source-id="${group.sourceId}">Cancel</button>
          </div>
        </div>`
          : `
        <div class="pl-1.5 border-l-2 border-slate-200 dark:border-slate-700 ml-0.5 py-1">
          <button type="button" class="add-dest-btn text-xs text-primary font-medium hover:underline" data-source-id="${group.sourceId}">+ Add destination</button>
        </div>`)
      : '';
    const destBlock = isCollapsed
      ? `<div class="py-0.5 pl-1"><span class="text-[10px] text-slate-500">${destCount} destination${destCount !== 1 ? 's' : ''}</span></div>`
      : `${arrowRow}${destRows}${addDestRow}`;
    const termsArr = termsBySource[group.sourceId] || [];
    const termsDisplay = escapeHtml(termsArr.join(', '));
    const excludedTermsBlock = isCollapsed ? '' : `
      <div class="pl-1.5 border-l-2 border-slate-200 dark:border-slate-700 ml-0.5 py-2 space-y-1.5">
        <div class="text-[10px] font-semibold text-slate-500 uppercase tracking-wider">Never translate these words</div>
        <p class="text-[10px] text-slate-500 dark:text-slate-400">Comma-separated (JSON import for terms with commas)</p>
        <textarea class="excluded-terms-input w-full rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-xs p-2 min-h-[56px] text-slate-900 dark:text-slate-100" data-source-id="${group.sourceId}" placeholder="BrandName, ProductName">${termsDisplay}</textarea>
        <div class="flex flex-wrap gap-1.5">
          <button type="button" class="excluded-terms-save px-2 py-1 rounded bg-primary text-white text-xs font-semibold" data-source-id="${group.sourceId}">Save</button>
          <button type="button" class="excluded-terms-export px-2 py-1 rounded border border-slate-300 dark:border-slate-600 text-xs" data-source-id="${group.sourceId}">Export</button>
          <button type="button" class="excluded-terms-import px-2 py-1 rounded border border-slate-300 dark:border-slate-600 text-xs" data-source-id="${group.sourceId}">Import</button>
        </div>
      </div>`;
    card.innerHTML = `<div class="flex flex-col gap-0">${sourceRow}${excludedTermsBlock}${destBlock}</div>`;
    container.appendChild(card);
  }

  // Empty card: "Add a Source" button (opens add-source view)
  const emptyCard = document.createElement('div');
  emptyCard.className = 'rounded-lg border-2 border-dashed border-slate-200 dark:border-slate-700 p-4 flex items-center justify-center min-h-[80px]';
  emptyCard.innerHTML = `
    <button type="button" id="btn-add-source-card" class="text-primary font-semibold flex items-center gap-2 hover:underline">
      <span class="material-symbols-outlined">add_circle</span> Add a Source
    </button>
  `;
  container.appendChild(emptyCard);

  // When "No translation" is selected in add-dest forms, disable image/poll options
  container.querySelectorAll('.add-dest-form').forEach(form => {
    const sel = form.querySelector('.add-dest-lang');
    if (sel && sel.value === 'none') {
      const imagesCb = form.querySelector('.add-dest-images');
      const pollCb = form.querySelector('.add-dest-poll');
      if (imagesCb) { imagesCb.disabled = true; imagesCb.checked = false; }
      if (pollCb) { pollCb.disabled = true; pollCb.checked = false; }
    }
  });
}

function bindDashboardEvents() {
  const container = document.getElementById('pair-cards');
  if (!container) return;

  container.addEventListener('change', async (e) => {
    if (e.target.classList.contains('add-dest-select')) {
      if (e.target.value === BROWSE_GROUPS_VALUE) {
        const sourceId = parseInt(e.target.dataset.sourceId, 10);
        e.target.value = '';
        openBrowseGroupsForDest(sourceId, null);
      }
      return;
    }
    if (e.target.classList.contains('add-dest-lang')) {
      const form = e.target.closest('.add-dest-form');
      if (!form) return;
      const imagesCb = form.querySelector('.add-dest-images');
      const pollCb = form.querySelector('.add-dest-poll');
      const isNone = e.target.value === 'none';
      if (imagesCb) { imagesCb.disabled = isNone; imagesCb.checked = false; }
      if (pollCb) { pollCb.disabled = isNone; pollCb.checked = false; }
      return;
    }
    if (e.target.classList.contains('rule-toggle')) {
      const id = parseInt(e.target.dataset.ruleId, 10);
      const enabled = e.target.checked;
      try {
        await fetch(API_BASE + '/rules/' + id, { method: 'PATCH', headers: apiHeaders(), body: JSON.stringify({ enabled }) });
        await loadRules();
        renderDashboard();
      } catch (err) { e.target.checked = !enabled; }
    }
    if (e.target.classList.contains('source-toggle')) {
      const sourceId = parseInt(e.target.dataset.sourceId, 10);
      const enabled = e.target.checked;
      try {
        await fetch(API_BASE + '/sources/' + sourceId + (enabled ? '/unpause' : '/pause'), { method: 'POST', headers: apiHeaders() });
        await loadRules();
        renderDashboard();
      } catch (err) { e.target.checked = !enabled; }
    }
  });

  container.addEventListener('click', async (e) => {
    const termsSave = e.target.closest('.excluded-terms-save');
    if (termsSave) {
      e.preventDefault();
      const sourceId = parseInt(termsSave.dataset.sourceId, 10);
      const ta = container.querySelector(`textarea.excluded-terms-input[data-source-id="${sourceId}"]`);
      if (!ta) return;
      const terms = parseCommaSeparatedTerms(ta.value);
      try {
        const res = await fetch(API_BASE + '/sources/' + sourceId + '/excluded-terms', {
          method: 'PUT',
          headers: apiHeaders(),
          body: JSON.stringify({ terms }),
        });
        if (res.ok) {
          await loadExcludedTerms();
          renderDashboard();
          logAction('Never translate words saved successfully', 'success', true);
        } else {
          const d = await res.json().catch(() => ({}));
          const errMsg = d.detail || 'Failed to save never translate words';
          logAction(errMsg, 'error', true);
          alert(errMsg);
        }
      } catch (err) {
        logAction('Failed to save never translate words', 'error', true);
        alert('Failed to save never translate words');
      }
      return;
    }
    const termsExport = e.target.closest('.excluded-terms-export');
    if (termsExport) {
      e.preventDefault();
      const sourceId = parseInt(termsExport.dataset.sourceId, 10);
      const ta = container.querySelector(`textarea.excluded-terms-input[data-source-id="${sourceId}"]`);
      const termsFromUi = ta ? parseCommaSeparatedTerms(ta.value) : (termsBySource[sourceId] || []);
      const doc = {
        format: 'teletranslate-excluded-terms',
        version: 1,
        exported_at: new Date().toISOString(),
        source_id: sourceId,
        terms: termsFromUi,
      };
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'excluded-terms-' + sourceId + '.json';
      a.click();
      URL.revokeObjectURL(a.href);
      logAction('Never translate words exported', 'success', true);
      return;
    }
    const termsImport = e.target.closest('.excluded-terms-import');
    if (termsImport) {
      e.preventDefault();
      pendingExcludedTermsImportSourceId = parseInt(termsImport.dataset.sourceId, 10);
      const fi = document.getElementById('excluded-terms-import-file');
      if (fi) fi.click();
      logAction('Import file picker opened for never translate words', 'info', false);
      return;
    }
    const collapseBtn = e.target.closest('.source-collapse');
    if (collapseBtn) {
      e.preventDefault();
      const sid = parseInt(collapseBtn.dataset.sourceId, 10);
      if (collapsedSourceIds.has(sid)) collapsedSourceIds.delete(sid);
      else collapsedSourceIds.add(sid);
      sessionStorage.setItem('collapsedSources', JSON.stringify([...collapsedSourceIds]));
      renderDashboard();
      return;
    }
    const addDestBtn = e.target.closest('.add-dest-btn');
    if (addDestBtn) {
      e.preventDefault();
      addingForSourceId = parseInt(addDestBtn.dataset.sourceId, 10);
      renderDashboard();
      return;
    }
    const addDestCancel = e.target.closest('.add-dest-cancel');
    if (addDestCancel) {
      e.preventDefault();
      addingForSourceId = null;
      renderDashboard();
      return;
    }
    const addDestSave = e.target.closest('.add-dest-save');
    if (addDestSave) {
      e.preventDefault();
      const sourceId = parseInt(addDestSave.dataset.sourceId, 10);
      const sel = container.querySelector(`.add-dest-select[data-source-id="${sourceId}"]`);
      const langSel = container.querySelector(`.add-dest-lang[data-source-id="${sourceId}"]`);
      const imagesCb = container.querySelector(`.add-dest-images[data-source-id="${sourceId}"]`);
      const pollCb = container.querySelector(`.add-dest-poll[data-source-id="${sourceId}"]`);
      const destId = sel?.value;
      const lang = langSel?.value;
      if (!destId || !lang) { alert('Select group and language'); return; }
      const isNone = lang === 'none';
      try {
        const res = await fetch(API_BASE + '/rules', {
          method: 'POST',
          headers: apiHeaders(),
          body: JSON.stringify({
            source_id: sourceId,
            destination_group_id: parseInt(destId, 10),
            destination_language: lang,
            translate_images: isNone ? false : (imagesCb?.checked ?? false),
            translate_poll: isNone ? false : (pollCb?.checked ?? false),
            enabled: true,
          }),
        });
        if (res.ok) {
          addingForSourceId = null;
          await loadRules();
          await loadSources();
          await loadCredits();
          renderDashboard();
        } else {
          const d = await res.json().catch(() => ({}));
          alert(d.detail || 'Failed to add');
        }
      } catch (err) { alert('Failed to add'); }
      return;
    }
    const destEdit = e.target.closest('.dest-edit');
    if (destEdit) {
      e.preventDefault();
      const ruleId = parseInt(destEdit.dataset.ruleId, 10);
      openEditRuleModal(ruleId);
      return;
    }
    const destDelete = e.target.closest('.dest-delete');
    if (destDelete) {
      e.preventDefault();
      const ruleId = parseInt(destDelete.dataset.ruleId, 10);
      if (confirm('Delete this destination pair?')) {
        try {
          await fetch(API_BASE + '/rules/' + ruleId, { method: 'DELETE', headers: apiHeaders() });
          await loadRules();
          renderDashboard();
        } catch (err) { alert('Failed to delete'); }
      }
      return;
    }
    const sourceEdit = e.target.closest('.source-edit');
    if (sourceEdit) {
      e.preventDefault();
      const sourceId = parseInt(sourceEdit.dataset.sourceId, 10);
      openAddSourceViewForEdit(sourceId);
      return;
    }
    const sourceDelete = e.target.closest('.source-delete');
    if (sourceDelete) {
      e.preventDefault();
      const sourceId = parseInt(sourceDelete.dataset.sourceId, 10);
      const groupRules = rules.filter(r => r.source_id === sourceId);
      const destCount = groupRules.length;
      if (confirm(destCount ? `Delete this source and all ${destCount} destination(s)?` : 'Delete this source?')) {
        try {
          for (const r of groupRules) {
            await fetch(API_BASE + '/rules/' + r.id, { method: 'DELETE', headers: apiHeaders() });
          }
          await fetch(API_BASE + '/sources/' + sourceId, { method: 'DELETE', headers: apiHeaders() });
          await loadRules();
          await loadSources();
          renderDashboard();
        } catch (err) { alert('Failed to delete'); }
      }
      return;
    }
    const addSourceCardBtn = e.target.closest('#btn-add-source-card');
    if (addSourceCardBtn) {
      e.preventDefault();
      editingSourceId = null;
      selectedSource = null;
      document.getElementById('source-input').value = '';
      showSourceError('');
      document.getElementById('source-preview').classList.add('hidden');
      updateAddSourceViewUI();
      showView('add-source');
      return;
    }
  });
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

function openEditRuleModal(ruleId) {
  const rule = rules.find(r => r.id === ruleId);
  if (!rule) return;
  editingRuleId = ruleId;
  const destSelect = document.getElementById('edit-rule-dest');
  destSelect.innerHTML = '<option value="">Choose a destination...</option>' +
    `<option value="${BROWSE_GROUPS_VALUE}">Browse groups…</option>` +
    chatsWithAccess.filter(c => c.type === 'group' || c.type === 'supergroup').map(c =>
      `<option value="${c.id}" ${c.id === rule.destination_group_id ? 'selected' : ''}>${escapeHtml(c.title || c.id)} ${c.access ? '✓' : '✗'}</option>`
    ).join('');
  const lang = rule.destination_language || 'en';
  const isNone = lang === 'none';
  document.getElementById('edit-rule-lang').value = lang;
  document.getElementById('edit-rule-translate-images').checked = isNone ? false : (rule.translate_images === true);
  document.getElementById('edit-rule-translate-images').disabled = isNone;
  document.getElementById('edit-rule-translate-poll').checked = isNone ? false : (rule.translate_poll === true);
  document.getElementById('edit-rule-translate-poll').disabled = isNone;
  document.getElementById('modal-edit-rule').classList.remove('hidden');
  document.getElementById('modal-edit-rule').setAttribute('aria-hidden', 'false');
}

function closeEditRuleModal() {
  editingRuleId = null;
  const modal = document.getElementById('modal-edit-rule');
  if (modal) {
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
  }
}

async function saveEditRule() {
  if (editingRuleId == null) return;
  const destId = document.getElementById('edit-rule-dest').value;
  const lang = document.getElementById('edit-rule-lang').value;
  const isNone = lang === 'none';
  const translateImages = isNone ? false : document.getElementById('edit-rule-translate-images').checked;
  const translatePoll = isNone ? false : document.getElementById('edit-rule-translate-poll').checked;
  if (!destId || destId === BROWSE_GROUPS_VALUE || !lang) { alert('Select destination and language'); return; }
  const rule = rules.find(r => r.id === editingRuleId);
  if (!rule) return;
  try {
    const res = await fetch(API_BASE + '/rules/' + editingRuleId, {
      method: 'PUT',
      headers: apiHeaders(),
      body: JSON.stringify({
        source_id: rule.source_id,
        destination_group_id: parseInt(destId, 10),
        destination_language: lang,
        translate_images: translateImages,
        translate_poll: translatePoll,
        enabled: rule.enabled !== false,
      }),
    });
    if (res.ok) {
      await loadRules();
      renderDashboard();
      closeEditRuleModal();
    } else alert('Failed to save');
  } catch (e) { alert('Failed to save'); }
}

async function runBrowseReturnRefresh() {
  if (!tg.initData) return;
  const rid = sessionStorage.getItem(SESSION_PAIRING_RULE);
  const sid = sessionStorage.getItem(SESSION_PAIRING_SOURCE);
  if (rid == null && sid == null) return;
  try {
    // Bot may still be writing ChatCache when the WebView resumes; a few passes helps.
    for (let i = 0; i < 4; i++) {
      await loadChatsWithAccess();
      await loadRules();
      if (i < 3) await new Promise(r => setTimeout(r, 400));
    }
    if (rid != null) {
      const ruleId = parseInt(rid, 10);
      sessionStorage.removeItem(SESSION_PAIRING_RULE);
      if (sid != null) sessionStorage.removeItem(SESSION_PAIRING_SOURCE);
      addingForSourceId = null;
      renderDashboard();
      openEditRuleModal(ruleId);
      return;
    }
    if (sid != null) {
      addingForSourceId = parseInt(sid, 10);
      sessionStorage.removeItem(SESSION_PAIRING_SOURCE);
    }
    renderDashboard();
  } catch (e) {}
}

function setupBrowseReturnHandler() {
  if (setupBrowseReturnHandler._bound) return;
  setupBrowseReturnHandler._bound = true;
  let debounceTimer = null;
  const scheduleBrowseReturnRefresh = () => {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      runBrowseReturnRefresh();
    }, 200);
  };
  // Telegram WebView often skips visibilitychange when returning from openTelegramLink; focus/pageshow are more reliable.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') scheduleBrowseReturnRefresh();
  });
  window.addEventListener('focus', scheduleBrowseReturnRefresh);
  window.addEventListener('pageshow', scheduleBrowseReturnRefresh);
  if (typeof tg.onEvent === 'function') {
    try {
      tg.onEvent('viewportChanged', scheduleBrowseReturnRefresh);
    } catch (e) {}
  }
}

function bindEvents() {
  const headerCredits = document.getElementById('header-credits');
  if (headerCredits) headerCredits.addEventListener('click', () => showView('account'));

  document.querySelectorAll('.btn-buy-pack').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.getAttribute('data-price-key');
      if (key) startBillingCheckout(key);
    });
  });

  const sourceInput = document.getElementById('source-input');
  if (sourceInput) {
    sourceInput.addEventListener('input', () => showSourceError(''));
    sourceInput.addEventListener('focus', () => showSourceError(''));
  }

  const addSourceBack = document.getElementById('add-source-back');
  if (addSourceBack) addSourceBack.onclick = () => { editingSourceId = null; showView('dashboard'); };
  const btnVerify = document.getElementById('btn-verify-source');
  if (btnVerify) btnVerify.onclick = verifySource;
  const btnAddSourceSubmit = document.getElementById('btn-add-source-submit');
  if (btnAddSourceSubmit) btnAddSourceSubmit.onclick = submitAddOrEditSource;

  const logsBack = document.getElementById('logs-back');
  if (logsBack) logsBack.onclick = () => showView('dashboard');
  const accountBack = document.getElementById('account-back');
  if (accountBack) accountBack.onclick = () => showView('dashboard');

  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.onclick = () => showView(btn.dataset.view);
  });

  bindDashboardEvents();

  const excludedTermsFile = document.getElementById('excluded-terms-import-file');
  if (excludedTermsFile && !excludedTermsFile.dataset.bound) {
    excludedTermsFile.dataset.bound = '1';
    excludedTermsFile.addEventListener('change', async (e) => {
      const f = e.target.files[0];
      const sid = pendingExcludedTermsImportSourceId;
      pendingExcludedTermsImportSourceId = null;
      e.target.value = '';
      if (!f || sid == null) return;
      try {
        const text = await f.text();
        const parsed = JSON.parse(text);
        let terms;
        if (Array.isArray(parsed)) {
          terms = parsed.filter(x => typeof x === 'string');
        } else if (parsed && typeof parsed === 'object' && Array.isArray(parsed.terms)) {
          terms = parsed.terms.filter(x => typeof x === 'string');
        } else {
          alert('Invalid JSON: expected an array or { "terms": [...] }');
          return;
        }
        const res = await fetch(API_BASE + '/sources/' + sid + '/excluded-terms', {
          method: 'PUT',
          headers: apiHeaders(),
          body: JSON.stringify({ terms }),
        });
        if (res.ok) {
          await loadExcludedTerms();
          renderDashboard();
          logAction('Never translate words imported successfully', 'success', true);
        } else {
          const d = await res.json().catch(() => ({}));
          const errMsg = d.detail || 'Failed to import never translate words';
          logAction(errMsg, 'error', true);
          alert(errMsg);
        }
      } catch (err) {
        logAction('Invalid file or failed to import never translate words', 'error', true);
        alert('Invalid file or failed to import');
      }
    });
  }

  const editRuleCancel = document.getElementById('edit-rule-cancel');
  if (editRuleCancel) editRuleCancel.onclick = closeEditRuleModal;
  const editRuleSave = document.getElementById('edit-rule-save');
  if (editRuleSave) editRuleSave.onclick = saveEditRule;
  const editRuleDest = document.getElementById('edit-rule-dest');
  if (editRuleDest && !editRuleDest.dataset.browseBound) {
    editRuleDest.dataset.browseBound = '1';
    editRuleDest.addEventListener('change', () => {
      if (editRuleDest.value !== BROWSE_GROUPS_VALUE) return;
      const rid = editingRuleId;
      const rule = rules.find(r => r.id === rid);
      if (rule) editRuleDest.value = String(rule.destination_group_id);
      else editRuleDest.value = '';
      openBrowseGroupsForDest(null, rid);
    });
  }
  const editLangSel = document.getElementById('edit-rule-lang');
  if (editLangSel && !editLangSel.dataset.bound) {
    editLangSel.dataset.bound = '1';
    editLangSel.addEventListener('change', () => {
      const isNone = editLangSel.value === 'none';
      const imgCb = document.getElementById('edit-rule-translate-images');
      const pollCb = document.getElementById('edit-rule-translate-poll');
      if (imgCb) { imgCb.disabled = isNone; imgCb.checked = false; }
      if (pollCb) { pollCb.disabled = isNone; pollCb.checked = false; }
    });
  }

  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.onclick = () => {
      const t = btn.dataset.theme;
      localStorage.setItem(THEME_KEY, t);
      applyTheme(t);
    };
  });

  const accountReports = document.getElementById('account-receive-reports');
  if (accountReports) {
    accountReports.checked = settings.receive_reports_telegram;
    accountReports.onchange = async () => {
      const v = document.getElementById('account-receive-reports').checked;
      try {
        await fetch(API_BASE + '/me/settings', { method: 'PATCH', headers: apiHeaders(), body: JSON.stringify({ receive_reports_telegram: v }) });
        settings.receive_reports_telegram = v;
      } catch (e) {}
    };
  }

  const accountSpamEnabled = document.getElementById('account-spam-protection-enabled');
  if (accountSpamEnabled) {
    accountSpamEnabled.checked = settings.spam_protection_enabled;
    accountSpamEnabled.onchange = async () => {
      const v = accountSpamEnabled.checked;
      try {
        const r = await fetch(API_BASE + '/me/settings', { method: 'PATCH', headers: apiHeaders(), body: JSON.stringify({ spam_protection_enabled: v }) });
        if (r.ok) { const data = await r.json(); settings.spam_protection_enabled = data.spam_protection_enabled; }
      } catch (e) {}
    };
  }
  const accountSpamMax = document.getElementById('account-spam-max-messages');
  if (accountSpamMax) {
    accountSpamMax.value = settings.spam_max_messages;
    accountSpamMax.onchange = async () => {
      let v = parseInt(accountSpamMax.value, 10);
      v = Math.min(1000, Math.max(1, isNaN(v) ? 50 : v));
      accountSpamMax.value = v;
      try {
        const r = await fetch(API_BASE + '/me/settings', { method: 'PATCH', headers: apiHeaders(), body: JSON.stringify({ spam_max_messages: v }) });
        if (r.ok) { const data = await r.json(); settings.spam_max_messages = data.spam_max_messages; }
      } catch (e) {}
    };
  }
  const accountSpamWindow = document.getElementById('account-spam-window-minutes');
  if (accountSpamWindow) {
    accountSpamWindow.value = settings.spam_window_minutes;
    accountSpamWindow.onchange = async () => {
      let v = parseInt(accountSpamWindow.value, 10);
      v = Math.min(1440, Math.max(1, isNaN(v) ? 5 : v));
      accountSpamWindow.value = v;
      try {
        const r = await fetch(API_BASE + '/me/settings', { method: 'PATCH', headers: apiHeaders(), body: JSON.stringify({ spam_window_minutes: v }) });
        if (r.ok) { const data = await r.json(); settings.spam_window_minutes = data.spam_window_minutes; }
      } catch (e) {}
    };
  }
  setupBrowseReturnHandler();
}

function showSourceError(msg) {
  const el = document.getElementById('source-error');
  if (!el) return;
  el.textContent = msg || '';
  el.classList.toggle('hidden', !msg);
}

function setVerifySourceLoading(loading) {
  const btn = document.getElementById('btn-verify-source');
  const icon = document.getElementById('btn-verify-icon');
  const label = document.getElementById('btn-verify-label');
  const spin = document.getElementById('btn-verify-spinner');
  if (!btn) return;
  btn.disabled = loading;
  btn.setAttribute('aria-busy', loading ? 'true' : 'false');
  btn.classList.toggle('cursor-wait', loading);
  btn.classList.toggle('opacity-90', loading);
  if (icon) icon.classList.toggle('hidden', loading);
  if (spin) spin.classList.toggle('hidden', !loading);
  if (label) label.textContent = loading ? 'Verifying…' : 'Verify Source';
}

async function verifySource() {
  showSourceError('');
  const raw = document.getElementById('source-input').value.trim();
  if (!raw) return;
  setVerifySourceLoading(true);
  try {
    let found = null;
    const asId = raw.replace(/^@|t\.me\/|https?:\/\/t\.me\//i, '').trim();
    const num = parseInt(asId, 10);
    if (!isNaN(num)) {
      found = chatsWithAccess.find(c => c.id === num || String(c.id) === asId);
    }
    if (!found) {
      const asIdLower = asId.toLowerCase();
      found = chatsWithAccess.find(c => {
        if (String(c.id) === asId || String(c.id) === raw) return true;
        if (!c.title) return false;
        const t = c.title.toLowerCase();
        const tNorm = t.replace(/\s+/g, '_');
        return t.includes(asIdLower) || tNorm.includes(asIdLower) || tNorm === asIdLower;
      });
    }
    if (!found && /[a-zA-Z]/.test(asId)) {
      try {
        const res = await fetch(API_BASE + '/chats/resolve', { method: 'POST', headers: apiHeaders(), body: JSON.stringify({ username: asId }) });
        if (res.ok) {
          const resolved = await res.json();
          await loadChatsWithAccess();
          found = chatsWithAccess.find(c => c.id === resolved.id) || resolved;
        }
      } catch (e) { /* ignore */ }
    }
    if (!found) {
      showSourceError('Channel not found. Add the bot to the channel first, then refresh the app.');
      document.getElementById('source-input').focus();
      return;
    }
    selectedSource = { id: found.id, title: found.title || String(found.id), access: found.access };
    showSourceError('');
    document.getElementById('source-preview').classList.remove('hidden');
    document.getElementById('source-preview-card').innerHTML = `
      <p class="font-bold">${escapeHtml(selectedSource.title)}</p>
      <p class="text-xs text-slate-500">ID: ${selectedSource.id}</p>
      <p class="text-xs mt-2 ${selectedSource.access ? 'text-green-500' : 'text-red-500'}">${selectedSource.access ? 'Bot has access' : 'No access'}</p>
    `;
    updateAddSourceSubmitButton();
  } finally {
    setVerifySourceLoading(false);
  }
}

function updateAddSourceViewUI() {
  const titleEl = document.getElementById('add-source-title');
  const btnEl = document.getElementById('btn-add-source-submit');
  if (!titleEl || !btnEl) return;
  if (editingSourceId != null) {
    titleEl.textContent = 'Edit Source';
    btnEl.textContent = 'Save';
  } else {
    titleEl.textContent = 'Add New Source';
    btnEl.textContent = 'Add Source';
  }
}

function updateAddSourceSubmitButton() {
  const btn = document.getElementById('btn-add-source-submit');
  if (!btn) return;
  btn.textContent = (editingSourceId != null) ? 'Save' : 'Add Source';
}

function openAddSourceViewForEdit(sourceId) {
  editingSourceId = sourceId;
  const s = sources.find(s => s.source_id === sourceId);
  const c = chatsWithAccess.find(ch => ch.id === sourceId);
  let prefill = (s && s.title) || (c && (c.title || (c.username ? '@' + c.username : null))) || nameFor(sourceId);
  if (prefill) prefill = String(prefill).trim();
  document.getElementById('source-input').value = prefill || '';
  showSourceError('');
  document.getElementById('source-preview').classList.add('hidden');
  selectedSource = null;
  if (c) {
    selectedSource = { id: c.id, title: c.title || String(c.id), access: c.access };
    document.getElementById('source-preview').classList.remove('hidden');
    document.getElementById('source-preview-card').innerHTML = `
      <p class="font-bold">${escapeHtml(selectedSource.title)}</p>
      <p class="text-xs text-slate-500">ID: ${selectedSource.id}</p>
      <p class="text-xs mt-2 ${selectedSource.access ? 'text-green-500' : 'text-red-500'}">${selectedSource.access ? 'Bot has access' : 'No access'}</p>
    `;
  }
  updateAddSourceViewUI();
  showView('add-source');
}

async function submitAddOrEditSource() {
  if (!selectedSource) { alert('Verify the source first.'); return; }
  try {
    if (editingSourceId == null) {
      const res = await fetch(API_BASE + '/sources', {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify({ source_id: selectedSource.id, title: selectedSource.title }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); alert(d.detail || 'Failed to add source'); return; }
      await loadSources();
      await loadRules();
      editingSourceId = null;
      selectedSource = null;
      showView('dashboard');
      renderDashboard();
    } else {
      const res = await fetch(API_BASE + '/sources/' + editingSourceId, {
        method: 'PATCH',
        headers: apiHeaders(),
        body: JSON.stringify({ source_id: selectedSource.id, title: selectedSource.title }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); alert(d.detail || 'Failed to save'); return; }
      await loadSources();
      await loadRules();
      editingSourceId = null;
      selectedSource = null;
      showView('dashboard');
      renderDashboard();
    }
  } catch (e) { alert('Failed to save'); }
}

function savePair() {
  if (!selectedSource) return;
  const destId = document.getElementById('pair-dest-select').value;
  const lang = document.getElementById('pair-dest-lang').value;
  const translateImages = document.getElementById('pair-dest-translate-images').checked;
  const translatePoll = document.getElementById('pair-dest-translate-poll').checked;
  if (!destId || !lang) { alert('Select destination and language'); return; }
  fetch(API_BASE + '/rules', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({
      source_id: selectedSource.id,
      destination_group_id: parseInt(destId, 10),
      destination_language: lang,
      translate_images: translateImages,
      translate_poll: translatePoll,
      enabled: true,
    }),
  }).then(async res => {
    if (res.ok) {
      await loadRules();
      await loadCredits();
      renderDashboard();
      showView('dashboard');
    } else alert('Failed to save');
  }).catch(() => alert('Failed to save'));
}

async function loadLogs() {
  const list = document.getElementById('logs-list');
  list.innerHTML = '<p class="text-slate-500">Loading…</p>';
  try {
    const logsRes = await fetch(API_BASE + '/logs?limit=80', { headers: apiHeaders() });
    const logs = logsRes.ok ? await logsRes.json() : [];
    list.innerHTML = '';
    const groups = new Map();
    const ensureGroup = (groupKey, sourceId, sourceName, sourceLink) => {
      const key = String(groupKey);
      if (!groups.has(key)) {
        groups.set(key, {
          group_key: key,
          source_id: sourceId,
          source_name: sourceName || String(sourceId),
          source_link: sourceLink || null,
          latest_created_at: null,
          destinations: [],
        });
      }
      return groups.get(key);
    };

    (Array.isArray(logs) ? logs : []).forEach(entry => {
      // Persisted logs don't have batch_id; source_link includes source message id and is stable per source message.
      const groupKey = `${entry.source_id}|${entry.source_link || entry.created_at || ''}`;
      const g = ensureGroup(groupKey, entry.source_id, entry.source_name, entry.source_link);
      if (entry.created_at && (!g.latest_created_at || new Date(entry.created_at).getTime() > new Date(g.latest_created_at).getTime())) {
        g.latest_created_at = entry.created_at;
      }
      g.destinations.push({
        dest_name: entry.dest_name,
        status: entry.status,
        error_message: entry.error_message,
        destination_link: entry.destination_link,
        cost_usd_cents: entry.cost_usd_cents ?? null,
        created_at: entry.created_at || null,
      });
    });

    const grouped = Array.from(groups.values()).sort((a, b) => {
      const ta = a.latest_created_at ? new Date(a.latest_created_at).getTime() : 0;
      const tb = b.latest_created_at ? new Date(b.latest_created_at).getTime() : 0;
      return tb - ta;
    });

    if (!grouped.length) { list.innerHTML = '<p class="text-slate-500">No logs yet.</p>'; return; }
    grouped.forEach(group => {
      const div = document.createElement('div');
      div.className = 'rounded-xl bg-white dark:bg-slate-900 p-4 border border-slate-100 dark:border-slate-800';
      const sourceTime = group.latest_created_at ? new Date(group.latest_created_at).toLocaleString() : '';
      group.destinations.sort((a, b) => {
        const ta = a.created_at ? new Date(a.created_at).getTime() : 0;
        const tb = b.created_at ? new Date(b.created_at).getTime() : 0;
        return tb - ta;
      });
      const destRows = group.destinations.map(d => {
        const status = d.status || '';
        const statusCl = status === 'Success' ? 'text-green-500' : 'text-red-500';
        const costStr = d.cost_usd_cents != null ? `$${(Number(d.cost_usd_cents) / 100).toFixed(2)}` : '—';
        const linkStr = d.destination_link ? `<a href="${escapeHtml(d.destination_link)}" target="_blank" rel="noopener" class="text-xs text-primary ml-2">View</a>` : '';
        return `
          <div class="grid grid-cols-[1.6fr_0.9fr_0.8fr_auto] items-center gap-2 py-1.5 text-sm border-b border-slate-100 dark:border-slate-800 last:border-b-0">
            <div class="min-w-0 text-slate-700 dark:text-slate-200 truncate">→ ${escapeHtml(d.dest_name)}</div>
            <div class="min-w-0 ${statusCl} font-medium truncate">${escapeHtml(status)}${d.error_message ? ': ' + escapeHtml(d.error_message) : ''}</div>
            <div class="text-slate-600 dark:text-slate-300 text-right">${costStr}</div>
            <div class="text-right">${linkStr}</div>
          </div>
        `;
      }).join('');
      div.innerHTML = `
        <div class="flex justify-between items-start gap-2">
          <div class="min-w-0">
            <p class="font-semibold text-base truncate">${escapeHtml(group.source_name)}</p>
            <p class="text-xs text-slate-500 mt-1">${sourceTime}</p>
          </div>
          <div class="flex flex-col gap-1 text-right shrink-0">
            ${group.source_link ? `<a href="${escapeHtml(group.source_link)}" target="_blank" rel="noopener" class="text-xs text-primary">Source</a>` : ''}
          </div>
        </div>
        <div class="mt-3 rounded-lg border border-slate-100 dark:border-slate-800">
          <div class="grid grid-cols-[1.6fr_0.9fr_0.8fr_auto] gap-2 px-2 py-1.5 text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400 border-b border-slate-100 dark:border-slate-800">
            <div>Destination</div>
            <div>Status</div>
            <div class="text-right">Cost</div>
            <div class="text-right"></div>
          </div>
          <div class="px-2">
            ${destRows}
          </div>
        </div>
      `;
      list.appendChild(div);
    });
  } catch (e) { list.innerHTML = '<p class="text-slate-500">Failed to load logs.</p>'; }
}

document.addEventListener('DOMContentLoaded', () => {
  actionLog = loadActionLog();
  ensureActionUi();
  logAction('Application loaded', 'success', false);
  const navViews = ['dashboard', 'logs', 'account'];
  const origShowView = showView;
  showView = (name) => {
    origShowView(name);
    if (name === 'logs') {
      loadLogs();
    }
    if (name === 'dashboard') {
      Promise.all([loadRules(), loadSources(), loadCredits(), loadChatsWithAccess()]).then(() => {
        renderDashboard();
        updateBalanceDisplay();
        runBrowseReturnRefresh();
      });
    }
    if (name === 'account') {
      loadSettings().then(() => {
        loadCredits().then(() => {
          updateBalanceDisplay();
          const arr = document.getElementById('account-receive-reports');
          if (arr) arr.checked = settings.receive_reports_telegram;
          const spamCb = document.getElementById('account-spam-protection-enabled');
          if (spamCb) spamCb.checked = settings.spam_protection_enabled;
          const spamMax = document.getElementById('account-spam-max-messages');
          if (spamMax) spamMax.value = settings.spam_max_messages;
          const spamWin = document.getElementById('account-spam-window-minutes');
          if (spamWin) spamWin.value = settings.spam_window_minutes;
          loadBillingLedger();
        });
      });
    }
  };
  init();
});
