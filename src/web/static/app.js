(() => {
  'use strict';

  function bootApp() {
  const byId = id => document.getElementById(id);
  if (!byId('quant-app')) return false;
  const compactLayout = window.matchMedia('(max-width: 900px)');
  const standaloneLayout = window.matchMedia('(display-mode: standalone)');
  const DEFAULT_VISIBLE_BARS = 63;
  const BUY_BOARD_FALLBACK_MS = 15_000;
  const PLANNING_LIST_MODES = new Set(['watchlist', 'buylist', 'buy_today']);
  const KANBAN_COLUMNS = Object.freeze([
    {key: 'BUYLIST', title: 'Buylist', short: 'Buylist', kicker: 'Planning'},
    {key: 'BUY_TODAY', title: 'Buy Today', short: 'Today', kicker: 'Monitoring'},
    {key: 'ENTRY_PENDING', title: 'Entry Pending', short: 'Entry', kicker: 'Broker lifecycle'},
    {key: 'OPEN_POSITION', title: 'Open Positions', short: 'Open', kicker: 'Position'},
    {key: 'PARTIAL_SELL', title: 'Partial Sell', short: 'Partial', kicker: 'Exit intent'},
    {key: 'SELL_ALL', title: 'Sell All', short: 'Sell All', kicker: 'Exit intent'},
  ]);
  const DISPLAY_PREFERENCE_DEFAULTS = Object.freeze({
    volume: true,
    ema10: true,
    ema20: true,
    ema50: true,
    relativeStrength: true,
    earnings: true,
    marketAlignment: true,
    adr: true,
    return1m: true,
    return3m: true,
  });
  const ORB_SETTINGS_DEFAULTS = Object.freeze({
    capital_min_percent: 10,
    capital_ideal_percent: 17.5,
    capital_max_percent: 30,
    stop_adr_min_percent: 15,
    stop_adr_ideal_percent: 65,
    stop_adr_max_percent: 90,
    ep_stop_adr_min_percent: 50,
    ep_stop_adr_ideal_percent: 100,
    ep_stop_adr_max_percent: 150,
  });

  function loadDisplayPreferences() {
    try {
      const saved = JSON.parse(localStorage.getItem('quant-web-display-preferences-v1') || '{}');
      return Object.fromEntries(Object.entries(DISPLAY_PREFERENCE_DEFAULTS).map(([key, fallback]) => (
        [key, typeof saved[key] === 'boolean' ? saved[key] : fallback]
      )));
    } catch (_error) {
      return {...DISPLAY_PREFERENCE_DEFAULTS};
    }
  }

  const state = {
    session: null,
    scanner: [],
    scannerSetup: '',
    scannerSetups: [],
    scannerTotal: 0,
    scannerLoading: false,
    planningRows: [],
    monitorRows: [],
    monitorFilter: 'all',
    monitorQuery: '',
    monitorLoading: false,
    monitorError: '',
    monitorPositionError: '',
    monitorAsOf: null,
    monitorEnabled: null,
    monitorTimer: null,
    monitorRetryTimer: null,
    monitorStaleSeconds: 180,
    buyTodayRows: [],
    buyBoardRows: [],
    buyBoardRevision: '',
    buyBoardNav: null,
    buyBoardNavAt: null,
    buyBoardNavNote: '',
    buyBoardColumn: 'BUYLIST',
    buyBoardLoading: false,
    buyBoardEpoch: 0,
    buyBoardError: '',
    buyBoardSelectedSymbol: '',
    buyBoardOptimistic: new Map(),
    buyBoardPendingActions: new Map(),
    buyBoardActionFeedback: new Map(),
    buyBoardTimer: null,
    historyRows: [],
    historyRange: {start: '', end: ''},
    historyLoaded: false,
    historyLoading: false,
    historyError: '',
    historyRequestToken: 0,
    visibleRows: [],
    listOrders: new Map(),
    listCursor: null,
    listScrollPositions: new Map(),
    manualRows: [],
    listMode: 'monitor',
    symbol: null,
    selectionToken: 0,
    timeframe: ['1D', '1H'].includes(localStorage.getItem('quant-web-timeframe'))
      ? localStorage.getItem('quant-web-timeframe')
      : '1D',
    split: false,
    plan: null,
    planningBusy: false,
    planningPendingSymbols: new Set(),
    planningEpochs: new Map(),
    optimisticPlans: new Map(),
    optimisticBuyToday: new Map(),
    operatorPendingCommands: new Map(),
    breakoutMode: false,
    breakoutDraftPrice: null,
    breakoutDragging: null,
    breakoutSaving: false,
    breakoutFollowup: null,
    drawings: [],
    drawMode: false,
    drawAnchor: null,
    selectedDrawingId: null,
    drawingDrag: null,
    drawingSaving: false,
    bundleCache: new Map(),
    bundleInflight: new Map(),
    headerMetricsBySymbol: new Map(),
    alignmentOpen: false,
    marketAlignment: null,
    mobilePage: 'chart',
    displayPreferences: loadDisplayPreferences(),
    orbSettings: null,
    orbSettingsRevision: 0,
    orbSettingsLoading: false,
    orbSettingsSaving: false,
    stockRows: new Map(),
    prefetchTimer: null,
    statusTimer: null,
    clientTimings: [],
    liveUpdateSocket: null,
    liveUpdateReconnectTimer: null,
    liveUpdateRefreshTimer: null,
    planningRevision: null,
    buyTodayDraftRevision: null,
    watchlistSessionDate: null,
    rolloverRefreshing: false,
    panes: {},
  };

  function setGlobal(message) { byId('global-message').textContent = message; }

  function recordClientTiming(name, startedAt, details = {}) {
    const sample = {
      name,
      duration_ms: Math.round((performance.now() - startedAt) * 100) / 100,
      recorded_at: new Date().toISOString(),
      ...details,
    };
    state.clientTimings.push(sample);
    if (state.clientTimings.length > 100) state.clientTimings.shift();
    return sample;
  }

  window.__quantWebMetrics = Object.freeze({
    snapshot: () => state.clientTimings.map(sample => ({...sample})),
    clear: () => { state.clientTimings.length = 0; },
  });

  function nextPaint() {
    return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }

  function planningPending(symbol = state.symbol) {
    return Boolean(symbol && state.planningPendingSymbols.has(symbol));
  }

  function operatorPending(symbol = state.symbol) {
    return Boolean(symbol && state.operatorPendingCommands.has(symbol));
  }

  function planningEpoch(symbol = state.symbol) {
    return Number(state.planningEpochs.get(symbol) || 0);
  }

  function bumpPlanningEpoch(symbol = state.symbol) {
    state.planningEpochs.set(symbol, planningEpoch(symbol) + 1);
  }

  function saveDisplayPreferences() {
    localStorage.setItem(
      'quant-web-display-preferences-v1',
      JSON.stringify(state.displayPreferences),
    );
  }

  function syncDisplaySettingInputs() {
    document.querySelectorAll('[data-display-setting]').forEach(input => {
      input.checked = Boolean(state.displayPreferences[input.dataset.displaySetting]);
    });
  }

  function applyHeaderDataVisibility() {
    byId('adr-chip').hidden = !state.displayPreferences.adr;
    byId('growth-1m-chip').hidden = !state.displayPreferences.return1m;
    byId('growth-3m-chip').hidden = !state.displayPreferences.return3m;
    byId('earnings-chip').hidden = !state.displayPreferences.earnings;
    const alignment = byId('market-alignment-overlay');
    alignment.hidden = !state.displayPreferences.marketAlignment || alignment.dataset.ready !== '1';
  }

  function applyDisplayPreferences(announce = false) {
    syncDisplaySettingInputs();
    applyHeaderDataVisibility();
    Object.values(state.panes).forEach(applyPaneDisplayPreferences);
    if (announce) setGlobal('Chart display settings saved on this device.');
  }

  function lockPageDragging() {
    const interactiveSurfaces = '.chart-surface, .rs-surface, .mobile-page, .mobile-board-list, .mobile-list-items, .mobile-symbol-search-results, .display-settings-scroll, .stock-list, .planning-panel';
    const shouldLock = () => compactLayout.matches || standaloneLayout.matches;
    document.addEventListener('touchmove', event => {
      if (!shouldLock()) return;
      const target = event.target instanceof Element ? event.target : null;
      if (!target?.closest(interactiveSurfaces)) event.preventDefault();
    }, {passive: false});
    document.addEventListener('dragstart', event => {
      if (shouldLock()) event.preventDefault();
    });
  }

  function localDateValue(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function initializeHistoryRange() {
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - 13);
    state.historyRange = {start: localDateValue(start), end: localDateValue(end)};
    syncHistoryInputs();
  }

  function syncHistoryInputs() {
    ['rail', 'mobile'].forEach(scope => {
      byId(`${scope}-history-from`).value = state.historyRange.start;
      byId(`${scope}-history-to`).value = state.historyRange.end;
    });
  }

  async function prepareServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map(registration => {
      const worker = registration.active || registration.waiting || registration.installing;
      if (!worker) return registration.unregister();
      const path = new URL(worker.scriptURL).pathname;
      return path === '/service-worker.js' ? registration.update() : registration.unregister();
    }));
    await navigator.serviceWorker.register('/service-worker.js', {
      scope: '/',
      updateViaCache: 'none',
    });
  }
  function setPanel(message, kind = '') {
    const target = byId('plan-message');
    target.textContent = message;
    target.className = `panel-message ${kind}`.trim();
    const quick = byId('quick-plan-message');
    quick.textContent = message;
    quick.className = `quick-plan-message ${kind}`.trim();
    quick.hidden = !message;
  }

  async function api(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      headers.set('X-CSRF-Token', state.session?.csrf_token || '');
    }
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, {...options, method, headers, credentials: 'same-origin'});
    if (response.status === 401) {
      window.location.replace('/login');
      throw new Error('Session expired.');
    }
    if (response.status === 304) return {notModified: true};
    const result = await response.json().catch(error => {
      if (error.name === 'AbortError') throw error;
      return {};
    });
    if (!response.ok) {
      const error = new Error(result.detail || `Request failed (${response.status})`);
      error.status = response.status;
      error.current = result.current;
      throw error;
    }
    return result;
  }

  function compactStatusWord(value, fallback) {
    const text = String(value || fallback || '').trim().toLowerCase();
    return text ? `${text[0].toUpperCase()}${text.slice(1)}` : '';
  }

  function setStatusDot(id, tone) {
    const dot = byId(id);
    dot.classList.remove('good', 'stale', 'closed');
    if (tone) dot.classList.add(tone);
  }

  let marketClockSchedule = null;
  let marketClockTimer = null;

  function renderMarketCountdown(market = null) {
    if (market?.as_of) {
      const schedule = {
        asOf: Date.parse(market.as_of),
        open: Date.parse(market.session_open),
        close: Date.parse(market.session_close),
        nextOpen: Date.parse(market.next_session_open),
        nextClose: Date.parse(market.next_session_close),
        receivedAt: performance.now(),
      };
      marketClockSchedule = [schedule.asOf, schedule.open, schedule.close].every(Number.isFinite)
        ? schedule : null;
    }
    const now = marketClockSchedule
      ? marketClockSchedule.asOf + performance.now() - marketClockSchedule.receivedAt
      : Date.now();
    let open = marketClockSchedule?.open;
    let close = marketClockSchedule?.close;
    if (now >= close) {
      open = marketClockSchedule.nextOpen;
      close = marketClockSchedule.nextClose;
    }
    const hasSchedule = Number.isFinite(open) && Number.isFinite(close) && now < close;
    const minutesUntil = boundary => Math.max(1, Math.ceil((boundary - now) / 60_000));
    const label = hasSchedule
      ? (now < open ? `Open in ${minutesUntil(open)}m` : `Close in ${minutesUntil(close)}m`)
      : 'Market —';
    const countdown = byId('market-countdown');
    if (countdown.textContent !== label) countdown.textContent = label;
    countdown.title = hasSchedule
      ? `NYSE regular session · Open ${new Date(open).toLocaleString('en-GB', {timeZone: 'Asia/Seoul'})} KST · Close ${new Date(close).toLocaleString('en-GB', {timeZone: 'Asia/Seoul'})} KST`
      : 'Waiting for the NYSE session schedule';
  }

  function renderStatusStrip(payload = {}) {
    const market = payload.market_status || state.session?.market_status || {};
    renderMarketCountdown(market);
    const longLabel = market.label || 'Market Status: Checking';
    const compactLabel = market.compact_label || 'Market Checking';
    byId('market-status-long').textContent = longLabel;
    byId('market-status-compact').textContent = compactLabel;
    byId('market-summary').title = market.as_of
      ? `${longLabel} · NYSE as of ${market.as_of}`
      : longLabel;
    setStatusDot('market-dot', market.state === 'OPEN' ? 'good' : (market.state === 'CLOSED' ? 'closed' : ''));

    const browserState = String(payload.browser_connection || 'CONNECTED').toUpperCase();
    byId('browser-status').title = `Browser: ${browserState}`;
    setStatusDot('browser-dot', browserState === 'CONNECTED' ? 'good' : 'closed');

    const webState = String(payload.web_service || 'HEALTHY').toUpperCase();
    byId('web-status').title = `Web: ${webState}`;
    setStatusDot('service-dot', webState === 'HEALTHY' ? 'good' : 'closed');

    const dataMode = String(payload.data_mode || state.session?.data_mode || 'DEMO').toUpperCase();
    byId('data-status').textContent = dataMode === 'PC_MIRROR' ? 'PC' : compactStatusWord(dataMode, 'DEMO');
    const dataFreshness = String(payload.data_freshness || '').toUpperCase();
    byId('data-summary').title = `Data mode: ${dataMode}${dataFreshness ? ` · ${dataFreshness}` : ''}`;
    const dataState = String(payload.data_host || 'AVAILABLE').toUpperCase();
    setStatusDot('data-dot', dataState === 'AVAILABLE' ? 'good' : 'closed');

    const executorState = String(payload.executor_health || 'UNKNOWN').toUpperCase();
    const executorReason = payload.executor_reason || '';
    const executorHost = payload.executor_hostname || '';
    byId('executor-status').title = `Executor: ${executorState}${executorHost ? ` · ${executorHost}` : ''}${executorReason ? ` · ${executorReason}` : ''}`;
    setStatusDot('executor-dot', executorState === 'HEALTHY' ? 'good'
      : (['UNKNOWN', 'STARTING', 'STANDBY', 'STANDBY_READY', 'SHUTTING_DOWN'].includes(executorState) ? 'stale' : 'closed'));
    if (payload.operator && state.session) state.session.operator = payload.operator;
    renderMobileWorkspace();
  }

  function setBadge(target, text, tone = '') {
    target.textContent = text;
    if (tone) target.dataset.tone = tone;
    else delete target.dataset.tone;
  }

  function marketLabel() {
    return state.session?.market_status?.compact_label
      || state.session?.market_status?.label
      || 'Market checking';
  }

  function renderSummaryPage() {
    const operator = state.session?.operator || {};
    const marketState = String(state.session?.market_status?.state || 'UNKNOWN').toUpperCase();
    setBadge(
      byId('summary-market-state'),
      marketState === 'OPEN' ? 'Open' : marketState === 'CLOSED' ? 'Closed' : 'Checking',
      marketState === 'OPEN' ? '' : marketState === 'CLOSED' ? 'warning' : 'danger',
    );
    byId('summary-session-label').textContent = marketLabel();
    byId('summary-control-label').textContent = operator.delegated
      ? `${operator.operator_control || 'Operator Control'} owns manual actions`
      : operator.reason || 'Mobile operator control unavailable';
    byId('operator-control-current').textContent = operator.operator_control
      ? `${operator.operator_control} selected`
      : 'Operator Control unavailable';
    byId('operator-control-executor').textContent = `Executor: ${operator.execution_owner || 'unknown'}`;
    const operatorTargets = byId('operator-control-targets');
    const targetFragment = document.createDocumentFragment();
    (operator.targets || []).forEach(target => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.operatorTarget = target.key;
      button.classList.toggle('active', Boolean(target.selected));
      button.disabled = !target.available || Boolean(target.selected);
      button.setAttribute('aria-pressed', String(Boolean(target.selected)));
      button.title = target.available
        ? `${target.label} ${target.state || 'ready'}`
        : target.reason || `${target.label} unavailable`;
      const label = document.createElement('strong');
      label.textContent = target.label;
      const detail = document.createElement('small');
      detail.textContent = target.selected
        ? 'Selected'
        : target.available
        ? String(target.state || 'Ready').replaceAll('_', ' ')
        : 'Offline';
      button.append(label, detail);
      button.addEventListener('click', () => changeOperatorControl(target));
      targetFragment.appendChild(button);
    });
    operatorTargets.replaceChildren(targetFragment);
    byId('summary-scanner-count').textContent = String(state.scannerTotal || state.scanner.length);
    byId('summary-watchlist-count').textContent = String(
      state.planningRows.filter(row => row.watchlist_member).length
    );
    byId('summary-buylist-count').textContent = String(
      state.planningRows.filter(row => row.buylist_member).length
    );
    byId('summary-buy-today-count').textContent = String(state.buyTodayRows.length);
    byId('summary-scanner-setup').textContent = byId('scanner-setup').textContent || 'Current scanner';
    byId('summary-scanner-freshness').textContent = byId('scanner-freshness').textContent || '—';
    const setupPicker = byId('summary-scanner-setups');
    const setupFragment = document.createDocumentFragment();
    state.scannerSetups.forEach(name => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = name;
      button.classList.toggle('active', name === state.scannerSetup);
      button.disabled = state.scannerLoading;
      button.addEventListener('click', () => {
        if (name !== state.scannerSetup) void loadScanner(name);
      });
      setupFragment.appendChild(button);
    });
    setupPicker.replaceChildren(setupFragment);
    renderOrbSettingsAccess();
  }

  const ORB_INPUTS = Object.freeze({
    capital_min_percent: 'orb-capital-min',
    capital_ideal_percent: 'orb-capital-ideal',
    capital_max_percent: 'orb-capital-max',
    stop_adr_min_percent: 'orb-stop-adr-min',
    stop_adr_ideal_percent: 'orb-stop-adr-ideal',
    stop_adr_max_percent: 'orb-stop-adr-max',
    ep_stop_adr_min_percent: 'orb-ep-stop-adr-min',
    ep_stop_adr_ideal_percent: 'orb-ep-stop-adr-ideal',
    ep_stop_adr_max_percent: 'orb-ep-stop-adr-max',
  });

  function canEditOrbSettings() {
    const operator = state.session?.operator || {};
    return Boolean(
      state.session?.mode === 'CONNECTED'
      && operator.delegated
      && (operator.operations || []).includes('update_orb_settings')
    );
  }

  function renderOrbSettingsAccess() {
    const editable = canEditOrbSettings();
    const busy = state.orbSettingsLoading || state.orbSettingsSaving;
    Object.values(ORB_INPUTS).forEach(id => { byId(id).disabled = busy || !editable; });
    byId('orb-settings-defaults').disabled = busy || !editable;
    byId('orb-settings-save').disabled = busy || !editable;
    byId('orb-settings-revision').textContent = state.orbSettingsRevision > 0
      ? `Shared r${state.orbSettingsRevision}`
      : state.orbSettingsLoading ? 'Loading' : 'Not synced';
  }

  function setOrbSettingsInputs(settings) {
    Object.entries(ORB_INPUTS).forEach(([name, id]) => {
      byId(id).value = Number(settings[name]).toFixed(2);
      byId(id).setCustomValidity('');
    });
  }

  function readOrbSettingsInputs() {
    const values = Object.fromEntries(
      Object.entries(ORB_INPUTS).map(([name, id]) => [name, Number(byId(id).value)])
    );
    if (!Object.values(values).every(Number.isFinite)) {
      throw new Error('Enter a number in every ORB setting.');
    }
    if (values.capital_min_percent < 0 || values.capital_max_percent > 100) {
      throw new Error('Capital allocation bounds must be between 0% and 100%.');
    }
    if (!(values.capital_min_percent <= values.capital_ideal_percent
      && values.capital_ideal_percent <= values.capital_max_percent)
      || values.capital_min_percent === values.capital_max_percent) {
      throw new Error('Capital ideal must be between distinct lower and upper bounds.');
    }
    for (const prefix of ['', 'ep_']) {
      const lower = values[`${prefix}stop_adr_min_percent`];
      const ideal = values[`${prefix}stop_adr_ideal_percent`];
      const upper = values[`${prefix}stop_adr_max_percent`];
      if (lower < 0 || !(lower <= ideal && ideal <= upper) || lower === upper) {
        throw new Error(`${prefix ? 'EP' : 'Normal'} Stop / ADR ideal must be between distinct non-negative bounds.`);
      }
    }
    return values;
  }

  async function loadOrbSettings() {
    const status = byId('orb-settings-status');
    if (state.session?.mode !== 'CONNECTED') {
      state.orbSettings = {...ORB_SETTINGS_DEFAULTS};
      setOrbSettingsInputs(state.orbSettings);
      status.textContent = 'Shared settings are available in connected mode.';
      renderOrbSettingsAccess();
      return;
    }
    if (state.orbSettingsLoading) return;
    state.orbSettingsLoading = true;
    renderOrbSettingsAccess();
    try {
      const result = await api('/api/v1/operator/orb-settings');
      state.orbSettings = {...ORB_SETTINGS_DEFAULTS, ...result.settings};
      state.orbSettingsRevision = Number(result.revision || 0);
      setOrbSettingsInputs(state.orbSettings);
      status.textContent = canEditOrbSettings()
        ? 'Synced. Changes apply to mobile, PC, and laptop.'
        : 'Read only until this device has Operator Control.';
      status.className = `mobile-inline-status ${canEditOrbSettings() ? 'success' : ''}`.trim();
    } catch (error) {
      status.textContent = `Shared settings unavailable: ${error.message}`;
      status.className = 'mobile-inline-status error';
    } finally {
      state.orbSettingsLoading = false;
      renderOrbSettingsAccess();
    }
  }

  async function saveOrbSettings(event) {
    event.preventDefault();
    if (state.orbSettingsSaving || !canEditOrbSettings()) return;
    const status = byId('orb-settings-status');
    let settings;
    try {
      settings = readOrbSettingsInputs();
    } catch (error) {
      status.textContent = error.message;
      status.className = 'mobile-inline-status error';
      return;
    }
    state.orbSettingsSaving = true;
    renderOrbSettingsAccess();
    status.textContent = 'Saving one shared revision...';
    status.className = 'mobile-inline-status';
    try {
      const result = await api('/api/v1/operator/orb-settings', {
        method: 'PUT',
        body: JSON.stringify({
          command_id: commandId(),
          expected_revision: state.orbSettingsRevision,
          ...settings,
        }),
      });
      state.orbSettings = {...result.settings};
      state.orbSettingsRevision = Number(result.revision || 0);
      setOrbSettingsInputs(state.orbSettings);
      status.textContent = `Saved as shared revision ${state.orbSettingsRevision}. PC and laptop refresh automatically.`;
      status.className = 'mobile-inline-status success';
    } catch (error) {
      if (error.status === 409 && error.current?.settings) {
        state.orbSettings = {...error.current.settings};
        state.orbSettingsRevision = Number(error.current.revision || 0);
        setOrbSettingsInputs(state.orbSettings);
      }
      status.textContent = `${error.status === 409 ? 'Settings changed on another device' : 'Save blocked'}: ${error.message}`;
      status.className = 'mobile-inline-status error';
    } finally {
      state.orbSettingsSaving = false;
      renderOrbSettingsAccess();
    }
  }

  async function changeOperatorControl(target) {
    if (!target?.available || target.selected) return;
    const status = byId('operator-control-status');
    const accepted = await confirmOperatorAction(
      `Move Operator Control to ${target.label}?`,
      'Manual actions will originate from the selected device. The Execution Owner and all broker safety gates remain unchanged.',
      `Use ${target.label}`,
    );
    if (!accepted) return;
    status.textContent = `Switching Operator Control to ${target.label}...`;
    status.className = 'mobile-inline-status';
    document.querySelectorAll('[data-operator-target]').forEach(button => { button.disabled = true; });
    try {
      const operator = await api('/api/v1/operator/control', {
        method: 'POST',
        body: JSON.stringify({target: target.key}),
      });
      state.session.operator = operator;
      status.textContent = `Operator Control is now ${target.label}.`;
      status.className = 'mobile-inline-status success';
      renderMobileWorkspace();
      void loadOrbSettings();
    } catch (error) {
      status.textContent = `Control was not changed: ${error.message}`;
      status.className = 'mobile-inline-status error';
      renderSummaryPage();
    }
  }

  function renderMarketPulsePage() {
    const alignment = state.marketAlignment || {};
    const states = alignment.states && typeof alignment.states === 'object'
      ? alignment.states : {};
    const marketState = String(state.session?.market_status?.state || 'UNKNOWN').toUpperCase();
    setBadge(
      byId('pulse-market-state'),
      marketState === 'OPEN' ? 'Market Open' : marketState === 'CLOSED' ? 'Market Closed' : 'Checking',
      marketState === 'OPEN' ? '' : 'warning',
    );
    byId('pulse-context').textContent = alignment.context_label || 'Context unavailable';
    byId('pulse-symbol-context').textContent = state.symbol
      ? `${state.symbol} · market, segment, sector, industry`
      : 'Select a stock to inspect leadership';
    byId('pulse-leadership').textContent = alignment.leadership_label
      ? `${alignment.score ?? '—'} ${alignment.leadership_label}`
      : 'Unavailable';
    byId('pulse-detail').textContent = alignment.stale
      ? 'The latest published market snapshot is stale.'
      : alignment.provisional
      ? 'This market context is provisional.'
      : 'Market, segment, sector, and industry context follow the selected stock.';
    const components = byId('pulse-components');
    const fragment = document.createDocumentFragment();
    ['MKT', 'SEG', 'SEC', 'IND'].forEach(label => {
      const item = document.createElement('div');
      item.className = 'mobile-pulse-component';
      const componentState = ['GREEN', 'YELLOW', 'RED'].includes(String(states[label] || '').toUpperCase())
        ? String(states[label]).toUpperCase() : 'UNKNOWN';
      item.dataset.state = componentState;
      const dot = document.createElement('i');
      dot.setAttribute('aria-hidden', 'true');
      const name = document.createElement('span');
      name.textContent = label;
      item.append(dot, name);
      fragment.appendChild(item);
    });
    components.replaceChildren(fragment);
  }

  function kanbanColumn(key) {
    return KANBAN_COLUMNS.find(column => column.key === key) || KANBAN_COLUMNS[0];
  }

  function boardStatusLabel(row) {
    if (row.board_status === 'BUY_TODAY') {
      return String(row.entry_runtime_status || 'Waiting for ORB').replaceAll('_', ' ');
    }
    if (row.board_status === 'ENTRY_PENDING') {
      if (row.entry_cancel_in_flight) return 'Cancelling entry';
      return row.entry_order_pending ? 'Order pending' : 'Entry pending';
    }
    if (row.board_status === 'OPEN_POSITION') {
      return String(row.position_runtime_status || 'Open').replaceAll('_', ' ');
    }
    if (row.board_status === 'PARTIAL_SELL') {
      if (row.exit_cancel_in_flight) return 'Cancelling partial sell';
      return row.exit_order_pending ? 'Order pending' : 'Partial sell requested';
    }
    if (row.board_status === 'SELL_ALL') {
      if (row.sell_all_at_market_open) return 'Queued for market open';
      if (row.exit_cancel_in_flight) return 'Cancelling / repricing';
      return row.exit_order_pending ? 'Order pending' : 'Sell all requested';
    }
    return row.breakout_price ? 'Breakout ready' : 'Breakout required';
  }

  function boardMetric(row) {
    if (['OPEN_POSITION', 'PARTIAL_SELL', 'SELL_ALL'].includes(row.board_status)) {
      const quantity = Number(row.broker_quantity || 0);
      const entry = Number(row.average_entry_price || 0);
      return quantity > 0
        ? `${quantity.toLocaleString()} @ ${entry > 0 ? entry.toFixed(2) : '—'}`
        : 'Quantity syncing';
    }
    const breakout = normalizeBreakoutPrice(row.breakout_price);
    return breakout === null ? 'No breakout' : `Breakout ${breakout.toFixed(2)}`;
  }

  function boardNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : null;
  }

  function boardMoney(value) {
    const number = boardNumber(value);
    return number === null ? '—' : `$${number.toFixed(number < 1 ? 4 : 2)}`;
  }

  function boardShares(value, unknownWhenZero = false) {
    const number = Math.max(0, Math.trunc(Number(value) || 0));
    return unknownWhenZero && !number ? 'Not sized' : `${number.toLocaleString()} sh`;
  }

  function boardAccountNav() {
    const nav = boardNumber(state.buyBoardNav);
    const age = (Date.now() - Date.parse(state.buyBoardNavAt || '')) / 1000;
    return nav !== null && Number.isFinite(age) && age >= -5 && age <= 900 ? nav : null;
  }

  function boardNavMetric(amount) {
    if (amount === null || !Number.isFinite(amount) || amount < 0) return 'Not sized';
    const nav = boardAccountNav();
    const percent = nav === null ? 'NAV unavailable' : `${(amount / nav * 100).toFixed(2)}% NAV`;
    const dollars = amount.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return `${percent} · $${dollars}`;
  }

  function boardRiskBudget(row) {
    const value = row.risk_percent;
    const fraction = value === null || value === undefined ? NaN : Number(value);
    return Number.isFinite(fraction) && fraction >= 0 && fraction <= 1
      ? `${(fraction * 100).toFixed(2)}% NAV` : 'Unavailable';
  }

  function boardTarget(row) {
    return Math.max(0, Number(row.broker_quantity) || 0,
      Number(row.target_position_quantity || row.planned_quantity) || 0);
  }

  function boardTime(value) {
    const time = Date.parse(value || '');
    return Number.isFinite(time) ? `${new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).format(time)} KST` : 'Time unavailable';
  }

  function boardPrice(row) {
    // Reuse observations already loaded by Monitor; never request broker quotes.
    const monitor = state.monitorRows.find(item => item.symbol === row.symbol);
    const candidates = [
      {price: boardNumber(row.last_reported_price), at: row.price_as_of, source: 'PC reported'},
      {price: boardNumber(monitor?.current_price), at: monitor?.quote_as_of,
        source: 'Yahoo · indicative', stale: Boolean(state.monitorError) || monitor?.quote_status !== 'CURRENT'},
    ].filter(item => item.price !== null);
    candidates.sort((a, b) => (Date.parse(b.at || '') || 0) - (Date.parse(a.at || '') || 0));
    if (!candidates.length) return {price: null, fresh: false, note: 'Price not reported yet'};
    const quote = candidates[0];
    const age = (Date.now() - Date.parse(quote.at || '')) / 1000;
    const fresh = Number.isFinite(age) && age >= -5
      && age <= state.monitorStaleSeconds && !quote.stale;
    const elapsed = Number.isFinite(age) && age >= 0
      ? age < 60 ? `${Math.floor(age)}s ago` : `${Math.floor(age / 60)}m ago`
      : 'Age unavailable';
    return {...quote, fresh, note: `${quote.source} · ${boardTime(quote.at)} · ${elapsed}${fresh ? '' : ' · stale'}`};
  }

  function boardDetails(row) {
    const price = boardPrice(row);
    if (row.board_status === 'BUYLIST') {
      return {price, facts: [['Price', boardMoney(price.price)], ['Breakout', boardMoney(row.breakout_price)],
        ['Risk budget', boardRiskBudget(row)], ['Planned allocation', 'Not sized']]};
    }
    const held = Math.max(0, Number(row.broker_quantity) || 0);
    const target = boardTarget(row);
    const position = ['OPEN_POSITION', 'PARTIAL_SELL', 'SELL_ALL'].includes(row.board_status);
    const stop = boardNumber(row.active_stop_price);
    const facts = [['Price', boardMoney(price.price)],
      [position ? 'Held' : 'Target shares', position ? boardShares(held) : boardShares(target, true)],
      [position ? 'Active stop' : 'Planned stop', position
        ? stop === null ? 'Not set' : boardMoney(stop)
        : boardMoney(row.entry_orb_low)]];
    if (!position) {
      facts.push(
        ['Risk budget', boardRiskBudget(row)],
        ['Breakout', boardMoney(row.breakout_price)],
        ['Entry plan', boardMoney(row.entry_execution_price || row.entry_trigger)],
        ['ORB', row.entry_orb_window || row.selected_orb_window || 'Not selected'],
      );
      if (row.entry_breakout_trigger) facts.push(['Entry trigger', boardMoney(row.entry_breakout_trigger)]);
      if (row.board_status === 'ENTRY_PENDING') {
        facts.push(['Held / target', `${held.toLocaleString()} / ${target > 0 ? target.toLocaleString() : '—'} sh`]);
        if (held > 0) facts.push(['Average fill', boardMoney(row.average_entry_price)]);
      }
      const entry = boardNumber(row.entry_execution_price || row.entry_trigger);
      const plannedStop = boardNumber(row.entry_orb_low);
      facts.push(['Planned allocation', boardNavMetric(entry !== null && target > 0 ? entry * target : null)]);
      if (entry !== null && plannedStop !== null && entry > plannedStop && target > 0) {
        facts.push(['Planned risk', boardNavMetric((entry - plannedStop) * target)]);
      }
      if (held > 0) {
        const average = boardNumber(row.average_entry_price);
        facts.push(['Allocated', boardNavMetric(average !== null ? average * held : null)]);
      }
      if (row.next_retry_at) facts.push(['Retry after', boardTime(row.next_retry_at)]);
    } else {
      facts.push(['Average entry', boardMoney(row.average_entry_price)],
        ['Sellable', boardShares(row.orderable_quantity)],
        ['Stop coverage', stop === null ? 'Not active'
          : `${Math.max(0, Number(row.stop_quantity) || 0).toLocaleString()} / ${held.toLocaleString()} sh`]);
      const average = boardNumber(row.average_entry_price);
      facts.push(['Allocated', boardNavMetric(average !== null && held > 0 ? average * held : null)],
        ['Risk at stop', stop !== null && held > 0
          && Number(row.stop_quantity || 0) >= held
          ? average !== null ? boardNavMetric(Math.max(0, average - stop) * held) : 'Unavailable'
          : 'Not fully covered']);
      if (price.price !== null && average !== null && held > 0) {
        const pnl = (price.price - average) * held;
        const percent = (price.price / average - 1) * 100;
        facts.push(['Est. P&L', `${pnl >= 0 ? '+' : '−'}$${Math.abs(pnl).toFixed(2)} (${percent >= 0 ? '+' : ''}${percent.toFixed(2)}%)${price.fresh ? '' : ' · stale'}`]);
      } else {
        facts.push(['Est. P&L', '—']);
      }
      if (price.price !== null && stop !== null) {
        facts.push(['To stop', `${((price.price - stop) / price.price * 100).toFixed(2)}%${price.fresh ? '' : ' · stale'}`]);
      }
      if (row.pending_stop_price) facts.push(['Pending stop',
        `${boardMoney(row.pending_stop_price)} · ${boardShares(row.pending_stop_quantity)}`]);
      if (row.board_status === 'PARTIAL_SELL') {
        facts.push(['Sell requested', boardShares(row.pending_partial_sell_quantity)]);
      }
      if (['PARTIAL_SELL', 'SELL_ALL'].includes(row.board_status)) {
        facts.push(['Working sell', row.exit_order_pending
          ? row.reserved_sell_quantity > 0 ? boardShares(row.reserved_sell_quantity) : 'Quantity syncing'
          : 'Not submitted']);
        if (row.next_exit_retry_at) facts.push(['Retry after', boardTime(row.next_exit_retry_at)]);
      }
      if (row.entry_remaining_target_quantity > 0) {
        facts.push(['Still to buy', boardShares(row.entry_remaining_target_quantity)]);
      }
    }
    return {facts, price};
  }

  function boardWarning(row) {
    const reason = ['PARTIAL_SELL', 'SELL_ALL'].includes(row.board_status)
      ? row.last_exit_error
      : ['BUY_TODAY', 'ENTRY_PENDING'].includes(row.board_status) ? row.entry_block_reason
        : row.board_status === 'BUYLIST' ? row.buy_today_note : '';
    return [reason, ...(row.warnings || [])].find(value => String(value || '').trim()) || '';
  }

  function mobileKanbanCard(row) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mobile-kanban-card';
    button.dataset.status = row.board_status;
    if (row._pendingAction) button.classList.add('pending');
    const top = document.createElement('span');
    top.className = 'mobile-kanban-card-top';
    const symbol = document.createElement('strong');
    symbol.textContent = row.symbol;
    if (row.is_ep) symbol.textContent += ' · EP';
    const priority = document.createElement('small');
    priority.textContent = row._pendingAction ? 'SYNCING' : `P${Number(row.kanban_priority || 0)}`;
    top.append(symbol, priority);
    const name = document.createElement('span');
    name.className = 'mobile-kanban-card-name';
    name.textContent = row.name || row.symbol;
    const facts = document.createElement('span');
    facts.className = 'mobile-kanban-card-facts';
    const status = document.createElement('small');
    status.textContent = boardStatusLabel(row);
    const metric = document.createElement('b');
    metric.textContent = boardMetric(row);
    facts.append(status, metric);
    button.append(top, name, facts);
    const details = boardDetails(row);
    const grid = document.createElement('span');
    grid.className = 'mobile-kanban-card-details';
    grid.append(...details.facts.map(([label, value]) => boardFact(label, value)));
    const priceNote = document.createElement('span');
    priceNote.className = 'mobile-kanban-price-note';
    priceNote.dataset.stale = String(!details.price.fresh);
    priceNote.textContent = details.price.note;
    button.append(grid, priceNote);
    const warning = boardWarning(row);
    if (warning) {
      const alert = document.createElement('span');
      alert.className = 'mobile-kanban-card-warning';
      alert.textContent = String(warning);
      button.appendChild(alert);
    }
    button.addEventListener('click', () => openBuyBoardActionSheet(row.symbol));
    return button;
  }

  function renderBuyBoardTabs() {
    const fragment = document.createDocumentFragment();
    KANBAN_COLUMNS.forEach(column => {
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('role', 'tab');
      const count = state.buyBoardRows.filter(row => row.board_status === column.key).length;
      button.className = column.key === state.buyBoardColumn ? 'active' : '';
      button.setAttribute('aria-selected', String(column.key === state.buyBoardColumn));
      const label = document.createElement('span');
      label.textContent = column.short;
      const badge = document.createElement('b');
      badge.textContent = String(count);
      button.append(label, badge);
      button.addEventListener('click', () => {
        state.buyBoardColumn = column.key;
        renderBuyBoardPage();
        byId('buy-board-cards').scrollTop = 0;
      });
      fragment.appendChild(button);
    });
    byId('buy-board-tabs').replaceChildren(fragment);
  }

  function renderBuyBoardPage() {
    const operator = state.session?.operator || {};
    const enabled = Boolean(operator.delegated);
    setBadge(
      byId('buy-board-authority'),
      enabled ? 'Operator ready' : 'Read only',
      enabled ? '' : 'warning',
    );
    byId('buy-board-control-state').textContent = enabled
      ? 'This phone is an authenticated delegated controller'
      : 'Mobile operator actions are unavailable';
    byId('buy-board-control-detail').textContent = enabled
      ? operator.same_device
        ? 'Commands commit on this Operator/Execution PC. No browser broker connection exists.'
        : operator.reason || 'Commands are queued for the Execution Owner.'
      : operator.reason || 'The hosting PC must own Operator Control.';
    renderBuyBoardTabs();
    const column = kanbanColumn(state.buyBoardColumn);
    const rows = state.buyBoardRows.filter(row => row.board_status === column.key);
    byId('buy-board-column-kicker').textContent = column.kicker;
    byId('buy-board-column-title').textContent = column.title;
    byId('buy-board-column-count').textContent = String(rows.length);
    const position = ['OPEN_POSITION', 'PARTIAL_SELL', 'SELL_ALL'].includes(column.key);
    const shares = rows.reduce((sum, row) => sum + (position
      ? Math.max(0, Number(row.broker_quantity) || 0) : boardTarget(row)), 0);
    const unsized = !position ? rows.filter(row => boardTarget(row) === 0).length : 0;
    byId('buy-board-column-summary').textContent = `${rows.length} stock${rows.length === 1 ? '' : 's'}${column.key === 'BUYLIST' ? '' : ` · ${shares.toLocaleString()} ${position ? 'held' : 'target'} shares${unsized ? ` · ${unsized} not sized` : ''}`}`;
    const nav = boardAccountNav();
    byId('buy-board-nav').textContent = nav === null
      ? state.buyBoardNavNote || 'Account NAV stale or unavailable'
      : `Account NAV $${nav.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    const cards = document.createDocumentFragment();
    rows.forEach(row => cards.appendChild(mobileKanbanCard(row)));
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'mobile-board-empty';
      empty.textContent = state.buyBoardLoading
        ? 'Loading canonical cards…'
        : state.buyBoardError || 'No cards in this column.';
      cards.appendChild(empty);
    }
    byId('buy-board-cards').replaceChildren(cards);
    const pending = state.buyBoardPendingActions.size;
    byId('buy-board-sync-status').textContent = pending
      ? `${pending} command${pending === 1 ? '' : 's'} waiting for canonical confirmation`
      : state.buyBoardError
        ? state.buyBoardError
        : state.buyBoardRevision
          ? 'Canonical PC board synced'
          : 'Loading canonical Kanban…';
    byId('buy-board-sync-status').className = `mobile-board-sync-status${state.buyBoardError ? ' error' : pending ? ' pending' : ''}`;
    const operations = new Set(operator.operations || []);
    const marketOpen = String(state.session?.market_status?.state || '').toUpperCase() === 'OPEN';
    const publish = byId('publish-today-plan');
    publish.disabled = !enabled || !operations.has('publish_today_plan') || marketOpen;
    publish.title = marketOpen
      ? 'Full plan publication is disabled during regular market hours.'
      : !enabled ? operator.reason || 'Operator Control is unavailable' : '';
  }

  function renderMobileWorkspace() {
    if (!byId('mobile-workspace')) return;
    renderSummaryPage();
    renderMarketPulsePage();
    renderBuyBoardPage();
  }

  function mergeBuyBoardOptimistic(rows) {
    const merged = [...rows];
    state.buyBoardOptimistic.forEach((entry, symbol) => {
      const index = merged.findIndex(row => row.symbol === symbol);
      if (index >= 0) merged.splice(index, 1);
      if (entry?.row) merged.push({...entry.row});
    });
    return merged.sort((a, b) => (
      Number(b.kanban_priority || 0) - Number(a.kanban_priority || 0)
      || String(a.symbol).localeCompare(String(b.symbol))
    ));
  }

  async function loadBuyBoard(silent = false) {
    if (state.buyBoardLoading || state.session?.mode === 'SANDBOX') return;
    const epoch = state.buyBoardEpoch;
    state.buyBoardLoading = true;
    if (!silent) renderBuyBoardPage();
    try {
      const result = await api('/api/v1/buyboard');
      if (epoch !== state.buyBoardEpoch) return;
      state.buyBoardRows = mergeBuyBoardOptimistic(result.rows || []);
      state.buyBoardRevision = String(result.revision || '');
      state.buyBoardNav = boardNumber(result.account_nav_usd);
      state.buyBoardNavAt = result.account_nav_as_of || null;
      state.buyBoardNavNote = String(result.account_nav_note || '');
      state.buyBoardError = '';
    } catch (error) {
      if (epoch === state.buyBoardEpoch) {
        state.buyBoardError = `Board sync unavailable: ${error.message}`;
      }
    } finally {
      state.buyBoardLoading = false;
      renderBuyBoardPage();
      if (!byId('buy-board-action-sheet').hidden && state.buyBoardSelectedSymbol) {
        renderBuyBoardActionSheet();
      }
      if (epoch !== state.buyBoardEpoch) void loadBuyBoard(true);
    }
  }

  function stopBuyBoardPolling() {
    if (state.buyBoardTimer !== null) window.clearInterval(state.buyBoardTimer);
    state.buyBoardTimer = null;
  }

  function startBuyBoardPolling() {
    stopBuyBoardPolling();
    void loadBuyBoard(false);
    state.buyBoardTimer = window.setInterval(
      () => loadBuyBoard(true),
      BUY_BOARD_FALLBACK_MS,
    );
  }

  function closeBuyBoardActionSheet() {
    state.buyBoardActionFeedback.delete(state.buyBoardSelectedSymbol);
    byId('buy-board-action-sheet').hidden = true;
    byId('buy-board-action-input').hidden = true;
    byId('buy-board-action-status').textContent = '';
    state.buyBoardSelectedSymbol = '';
  }

  function boardFact(label, value) {
    const item = document.createElement('span');
    const name = document.createElement('small');
    name.textContent = label;
    const result = document.createElement('b');
    result.textContent = value;
    item.append(name, result);
    return item;
  }

  function boardActionButton(label, action, options = {}) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.className = options.danger ? 'danger' : options.secondary ? 'secondary' : '';
    button.disabled = Boolean(options.disabled);
    if (options.title) button.title = options.title;
    button.addEventListener('click', () => {
      if (options.input) {
        const wrapper = byId('buy-board-action-input');
        const input = byId('buy-board-action-value');
        wrapper.hidden = false;
        wrapper.dataset.action = action;
        byId('buy-board-action-input-label').textContent = options.input.label;
        byId('buy-board-action-input-submit').textContent = options.input.submit;
        input.step = options.input.step;
        input.min = options.input.min;
        input.value = options.input.value || '';
        requestAnimationFrame(() => input.focus({preventScroll: true}));
        return;
      }
      void applyBuyBoardAction(action, options.payload || {});
    });
    return button;
  }

  function renderBuyBoardActionSheet() {
    const row = state.buyBoardRows.find(item => item.symbol === state.buyBoardSelectedSymbol);
    if (!row) {
      closeBuyBoardActionSheet();
      return;
    }
    const stage = kanbanColumn(row.board_status);
    byId('buy-board-action-stage').textContent = stage.title;
    byId('buy-board-action-symbol').textContent = row.symbol;
    byId('buy-board-action-name').textContent = row.name || row.symbol;
    const facts = byId('buy-board-action-facts');
    const details = boardDetails(row);
    const factRows = [boardFact('Status', boardStatusLabel(row)),
      ...details.facts.map(([label, value]) => boardFact(label, value))];
    facts.replaceChildren(...factRows);
    byId('buy-board-action-price-note').textContent = details.price.note;
    byId('buy-board-action-warning').textContent = boardWarning(row);
    const actions = byId('buy-board-action-buttons');
    const fragment = document.createDocumentFragment();
    const pending = state.buyBoardPendingActions.has(row.symbol);
    const cancellingEntry = row.entry_block_reason === 'cancel_requested' || row.entry_cancel_in_flight;
    if (row.board_status === 'BUYLIST') {
      fragment.append(
        boardActionButton('Activate Buy Today', 'activate_buy_today', {disabled: pending || !row.breakout_price}),
        boardActionButton('Remove from Buylist', 'remove_buylist', {secondary: true, disabled: pending}),
      );
    } else if (row.board_status === 'BUY_TODAY') {
      fragment.append(boardActionButton(cancellingEntry ? 'Cancelling…' : 'Return to Buylist', 'deactivate_buy_today', {secondary: true, disabled: pending || cancellingEntry}));
    } else if (row.board_status === 'ENTRY_PENDING') {
      fragment.append(boardActionButton(cancellingEntry ? 'Cancelling…' : 'Cancel Entry', 'cancel_entry', {danger: true, disabled: pending || cancellingEntry}));
    } else if (row.board_status === 'OPEN_POSITION') {
      if (Number(row.entry_remaining_target_quantity) > 0) {
        fragment.append(boardActionButton(cancellingEntry ? 'Cancelling…' : 'Cancel remaining buy', 'cancel_entry', {
          secondary: true, disabled: pending || cancellingEntry,
        }));
      }
      fragment.append(
        boardActionButton('Partial Sell', 'request_partial_sell', {
          disabled: pending,
          input: {label: 'Shares to sell', submit: 'Request', step: '1', min: '1'},
        }),
        boardActionButton('Sell All', 'request_sell_all', {danger: true, disabled: pending}),
      );
    } else if (row.board_status === 'PARTIAL_SELL') {
      fragment.append(boardActionButton('Cancel Partial Sell', 'cancel_partial_sell', {secondary: true, disabled: pending}));
    } else if (row.board_status === 'SELL_ALL') {
      fragment.append(boardActionButton('Cancel Sell All', 'cancel_sell_all', {
        secondary: true,
        disabled: pending || !row.can_cancel_sell_all || row.exit_order_pending,
        title: row.can_cancel_sell_all ? '' : 'Only an unsubmitted Sell All can be cancelled. Reconcile any pending SELL first.',
      }));
    }
    if (['OPEN_POSITION', 'PARTIAL_SELL'].includes(row.board_status)) {
      fragment.append(
        boardActionButton('Use ORB Low Stop', 'set_orb_stop', {secondary: true, disabled: pending || !row.entry_orb_low}),
        boardActionButton('Move Stop to Breakeven', 'set_breakeven_stop', {secondary: true, disabled: pending}),
        boardActionButton('Set Manual Stop', 'set_manual_stop', {
          secondary: true,
          disabled: pending,
          input: {label: 'Manual stop price', submit: 'Set stop', step: '0.01', min: '0.01'},
        }),
      );
    }
    const siblings = state.buyBoardRows.filter(item => item.board_status === row.board_status);
    const index = siblings.findIndex(item => item.symbol === row.symbol);
    fragment.append(
      boardActionButton('Move Up', 'reorder_card', {
        secondary: true, disabled: pending || index <= 0,
        payload: {target_priority: Number(siblings[index - 1]?.kanban_priority || 0) + 1},
      }),
      boardActionButton('Move Down', 'reorder_card', {
        secondary: true, disabled: pending || index < 0 || index >= siblings.length - 1,
        payload: {target_priority: Number(siblings[index + 1]?.kanban_priority || 0) - 1},
      }),
    );
    actions.replaceChildren(fragment);
    byId('buy-board-action-input').hidden = true;
    const feedback = state.buyBoardActionFeedback.get(row.symbol);
    byId('buy-board-action-status').textContent = pending
      ? 'Saving…' : feedback?.message || '';
    byId('buy-board-action-status').className = `mobile-inline-status ${pending ? '' : feedback?.kind || ''}`;
  }

  function openBuyBoardActionSheet(symbol) {
    state.buyBoardSelectedSymbol = symbol;
    renderBuyBoardActionSheet();
    byId('buy-board-action-sheet').hidden = false;
  }

  function optimisticBoardAction(row, action, payload) {
    const next = {...row, _pendingAction: action};
    if (action === 'activate_buy_today') next.is_ep = Boolean(payload.is_ep);
    const target = {
      activate_buy_today: 'BUY_TODAY',
      deactivate_buy_today: 'BUYLIST',
      request_partial_sell: 'PARTIAL_SELL',
      request_sell_all: 'SELL_ALL',
      cancel_sell_all: 'OPEN_POSITION',
    }[action];
    if (target) next.board_status = target;
    if (action === 'cancel_entry' && row.board_status === 'BUY_TODAY') next.board_status = 'BUYLIST';
    if (action === 'cancel_entry' && row.board_status === 'ENTRY_PENDING') next.entry_cancel_in_flight = true;
    if (action === 'cancel_partial_sell') {
      if (!row.exit_order_pending) next.board_status = 'OPEN_POSITION';
      else next.exit_cancel_in_flight = true;
    }
    if (action === 'request_partial_sell') next.pending_partial_sell_quantity = Number(payload.quantity || 0);
    if (action === 'request_sell_all') next.exit_all_required = true;
    if (action === 'reorder_card') next.kanban_priority = Number(payload.target_priority || 0);
    if (action === 'set_orb_stop') {
      next.pending_stop_type = 'ORB_LOW';
      next.pending_stop_price = row.entry_orb_low;
    }
    if (action === 'set_breakeven_stop') next.pending_stop_type = 'BREAKEVEN';
    if (action === 'set_manual_stop') {
      next.pending_stop_type = 'MANUAL_PRICE';
      next.pending_stop_price = Number(payload.price || 0);
    }
    return next;
  }

  async function reconcileBuyBoardCommand(symbol, requestId) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 2_000));
      let command;
      try {
        command = await api(`/api/v1/operator/commands/${encodeURIComponent(requestId)}`);
      } catch (_error) {
        continue;
      }
      if (!command.terminal) continue;
      state.buyBoardEpoch += 1;
      state.buyBoardPendingActions.delete(symbol);
      state.buyBoardOptimistic.delete(symbol);
      state.operatorPendingCommands.delete(symbol);
      state.buyBoardActionFeedback.set(symbol, {
        message: command.success ? 'Saved.' : `${command.status}: ${command.error || 'The change was not saved.'}`,
        kind: command.success ? 'success' : 'error',
      });
      await loadBuyBoard(false);
      if (state.buyBoardSelectedSymbol === symbol && !byId('buy-board-action-sheet').hidden) {
        byId('buy-board-action-status').textContent = command.success
          ? 'Confirmed by the Execution Owner.'
          : `${command.status}: ${command.error || 'No canonical change was committed.'}`;
        byId('buy-board-action-status').className = `mobile-inline-status ${command.success ? 'success' : 'error'}`;
      }
      return;
    }
  }

  async function applyBuyBoardAction(action, payload = {}) {
    const symbol = state.buyBoardSelectedSymbol;
    const row = state.buyBoardRows.find(item => item.symbol === symbol);
    if (!row || state.buyBoardPendingActions.has(symbol)) return;
    if (!operatorOperationEnabled(action)) {
      byId('buy-board-action-status').textContent = 'This operator action is not enabled.';
      byId('buy-board-action-status').className = 'mobile-inline-status error';
      return;
    }
    const confirmations = {
      activate_buy_today: [`Activate ${symbol} for Buy Today?`, 'This publishes executable entry intent. No order is placed now; the Execution Owner still enforces every runtime, risk, market-data, and broker gate.', 'Activate'],
      request_partial_sell: [`Request a partial sell for ${symbol}?`, `Request ${payload.quantity} share${Number(payload.quantity) === 1 ? '' : 's'} as durable exit intent.`, 'Request partial sell'],
      request_sell_all: [`Sell all of ${symbol}?`, 'This records durable liquidation intent. The Execution Owner may submit it only after the workflow gates pass.', 'Request Sell All'],
    };
    if (action === 'activate_buy_today') {
      const choice = await confirmBuyTodayPublication(symbol, confirmations[action][1]);
      if (!choice) return;
      payload = {...payload, is_ep: choice === 'ep'};
    } else if (confirmations[action]) {
      const accepted = await confirmOperatorAction(...confirmations[action]);
      if (!accepted) return;
    }
    const requestId = commandId();
    state.buyBoardEpoch += 1;
    state.buyBoardActionFeedback.delete(symbol);
    const optimistic = optimisticBoardAction(row, action, payload);
    state.buyBoardOptimistic.set(symbol, {row: optimistic});
    state.buyBoardPendingActions.set(symbol, {commandId: requestId, action});
    state.operatorPendingCommands.set(symbol, requestId);
    state.buyBoardRows = mergeBuyBoardOptimistic(state.buyBoardRows);
    if (optimistic) state.buyBoardColumn = optimistic.board_status;
    renderBuyBoardPage();
    if (!byId('buy-board-action-sheet').hidden) renderBuyBoardActionSheet();
    try {
      const result = await api('/api/v1/operator/board-actions', {
        method: 'POST',
        body: JSON.stringify({
          command_id: requestId,
          action,
          symbol,
          expected_revision: row.version,
          ...payload,
        }),
      });
      if (result.queued) {
        byId('buy-board-action-status').textContent = 'Saved immediately on this phone; queued for the Execution Owner.';
        byId('buy-board-action-status').className = 'mobile-inline-status success';
        void reconcileBuyBoardCommand(symbol, requestId);
      } else {
        state.buyBoardEpoch += 1;
        state.buyBoardPendingActions.delete(symbol);
        state.buyBoardOptimistic.delete(symbol);
        state.operatorPendingCommands.delete(symbol);
        const confirmedRows = state.buyBoardRows.filter(item => item.symbol !== symbol);
        if (result.card) confirmedRows.push(result.card);
        state.buyBoardRows = mergeBuyBoardOptimistic(confirmedRows);
        state.buyBoardRevision = String(result.board_revision || state.buyBoardRevision);
        state.buyBoardActionFeedback.set(symbol, {message: 'Saved.', kind: 'success'});
        renderBuyBoardPage();
        if (!byId('buy-board-action-sheet').hidden) renderBuyBoardActionSheet();
        // A failed follow-up read must not undo a confirmed canonical write.
        void loadBuyBoard(true);
        void refreshPlanningLists().catch(() => {});
      }
    } catch (error) {
      state.buyBoardEpoch += 1;
      state.buyBoardPendingActions.delete(symbol);
      state.buyBoardOptimistic.delete(symbol);
      state.operatorPendingCommands.delete(symbol);
      state.buyBoardRows = mergeBuyBoardOptimistic([
        ...state.buyBoardRows.filter(item => item.symbol !== symbol), row,
      ]);
      state.buyBoardActionFeedback.set(symbol, {
        message: `Not saved: ${error.message}`, kind: 'error',
      });
      renderBuyBoardPage();
      if (state.buyBoardSelectedSymbol === symbol && !byId('buy-board-action-sheet').hidden) {
        renderBuyBoardActionSheet();
      }
    }
  }

  function setMobilePage(page) {
    const next = ['summary', 'chart', 'buy-board'].includes(page)
      ? page : 'chart';
    state.mobilePage = next;
    byId('mobile-workspace').dataset.mobilePage = next;
    document.querySelectorAll('[data-mobile-page-panel]').forEach(panel => {
      panel.hidden = panel.dataset.mobilePagePanel !== next;
    });
    document.querySelectorAll('.mobile-workspace-tab').forEach(button => {
      const active = button.dataset.mobilePage === next;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    byId('mobile-list-popover').hidden = true;
    byId('mobile-menu-popover').hidden = true;
    byId('mobile-list-menu').setAttribute('aria-expanded', 'false');
    if (next === 'buy-board' && state.session) startBuyBoardPolling();
    else stopBuyBoardPolling();
    if (next !== 'chart') {
      setBreakoutMode(false, false);
      setDrawingMode(false);
    } else {
      requestAnimationFrame(() => Object.values(state.panes).forEach(pane => {
        if (!pane) return;
        positionBreakoutHandle(pane);
        renderPaneDrawings(pane);
      }));
    }
    renderMobileWorkspace();
  }

  let operatorConfirmResolve = null;
  function closeOperatorConfirm(accepted = false) {
    byId('operator-confirm-dialog').hidden = true;
    const resolve = operatorConfirmResolve;
    operatorConfirmResolve = null;
    if (resolve) resolve(accepted);
  }

  function confirmOperatorAction(title, copy, confirmLabel = 'Confirm', epChoice = false) {
    if (operatorConfirmResolve) closeOperatorConfirm(false);
    byId('operator-confirm-title').textContent = title;
    byId('operator-confirm-copy').textContent = copy;
    byId('operator-confirm-submit').textContent = confirmLabel;
    byId('operator-confirm-ep').hidden = !epChoice;
    byId('operator-confirm-ep').parentElement.classList.toggle('has-ep-choice', epChoice);
    byId('operator-confirm-dialog').hidden = false;
    return new Promise(resolve => { operatorConfirmResolve = resolve; });
  }

  function confirmBuyTodayPublication(symbol, copy) {
    const settings = {...ORB_SETTINGS_DEFAULTS, ...state.orbSettings};
    const bounds = prefix => ['min', 'ideal', 'max']
      .map(bound => `${Number(settings[`${prefix}stop_adr_${bound}_percent`])}%`).join(' / ');
    return confirmOperatorAction(
      `Publish ${symbol} to Buy Today?`,
      `${copy}\n\nStop/ADR lower / ideal / upper\nNormal: ${bounds('')}\nEP: ${bounds('ep_')}`,
      'Publish to Buy Today', true,
    );
  }

  async function refreshStatusStrip() {
    try {
      const payload = await api('/api/v1/status');
      const previousSessionDate = state.watchlistSessionDate;
      const nextSessionDate = String(payload.market_status?.watchlist_session_date || '');
      const previousPlanningRevision = state.planningRevision;
      const nextPlanningRevision = String(payload.planning_revision || '');
      const previousDraftRevision = state.buyTodayDraftRevision;
      const nextDraftRevision = String(payload.buy_today_draft_revision || '');
      renderStatusStrip(payload);
      if (state.session) {
        state.session.market_status = payload.market_status || state.session.market_status;
        if (typeof payload.planning_writable === 'boolean') {
          state.session.planning_writable = payload.planning_writable;
        }
        if (Array.isArray(payload.planning_operations)) {
          state.session.planning_operations = payload.planning_operations;
        }
        renderPlan();
      }
      if (nextSessionDate) state.watchlistSessionDate = nextSessionDate;
      if (nextPlanningRevision) state.planningRevision = nextPlanningRevision;
      if (nextDraftRevision) state.buyTodayDraftRevision = nextDraftRevision;
      if (previousSessionDate && nextSessionDate && previousSessionDate !== nextSessionDate) {
        await refreshAfterWatchlistRollover(previousSessionDate, nextSessionDate);
      } else if (
        previousPlanningRevision
        && nextPlanningRevision
        && previousPlanningRevision !== nextPlanningRevision
      ) {
        await refreshCanonicalPlanning();
      } else if (
        previousDraftRevision
        && nextDraftRevision
        && previousDraftRevision !== nextDraftRevision
      ) {
        await refreshCanonicalPlanning();
      }
    } catch (error) {
      setStatusDot('service-dot', 'closed');
      byId('web-status').title = `Web: UNAVAILABLE · ${error.message}`;
    }
  }

  async function refreshCanonicalPlanning() {
    if (state.rolloverRefreshing || planningPending() || state.breakoutSaving) return;
    state.rolloverRefreshing = true;
    try {
      await Promise.all([refreshPlanningLists(), loadBuyBoard(true)]);
      if (state.symbol) {
        const result = await api(`/api/v1/planning/${encodeURIComponent(state.symbol)}`);
        state.plan = state.optimisticPlans.get(state.symbol) || result.card;
        renderPlan();
      }
      setGlobal('PC planning changes synced to this device.');
    } catch (error) {
      setGlobal(`PC planning refresh failed: ${error.message}`);
    } finally {
      state.rolloverRefreshing = false;
    }
  }

  function scheduleLivePlanningRefresh(event) {
    if (event?.kind === 'intraday_monitor') {
      void loadIntradayMonitor();
      return;
    }
    const symbol = String(event?.symbol || '').toUpperCase();
    if (symbol && planningPending(symbol)) return;
    if (state.liveUpdateRefreshTimer !== null) {
      window.clearTimeout(state.liveUpdateRefreshTimer);
    }
    state.liveUpdateRefreshTimer = window.setTimeout(() => {
      state.liveUpdateRefreshTimer = null;
      if (event?.kind === 'operator_control') {
        void refreshStatusStrip();
        return;
      }
      if (event?.kind === 'orb_settings') {
        void loadOrbSettings();
        return;
      }
      void refreshCanonicalPlanning();
    }, 40);
  }

  function connectLiveUpdates() {
    const active = state.liveUpdateSocket;
    if (active && [WebSocket.CONNECTING, WebSocket.OPEN].includes(active.readyState)) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${window.location.host}/live-updates`);
    state.liveUpdateSocket = socket;
    socket.addEventListener('message', message => {
      let event;
      try { event = JSON.parse(message.data); } catch (_error) { return; }
      if (!event || ['ready', 'ping'].includes(event.kind)) return;
      scheduleLivePlanningRefresh(event);
    });
    socket.addEventListener('close', () => {
      if (state.liveUpdateSocket === socket) state.liveUpdateSocket = null;
      if (state.liveUpdateReconnectTimer !== null) {
        window.clearTimeout(state.liveUpdateReconnectTimer);
      }
      state.liveUpdateReconnectTimer = window.setTimeout(() => {
        state.liveUpdateReconnectTimer = null;
        if (state.session) connectLiveUpdates();
      }, 1_000);
    });
  }

  async function refreshAfterWatchlistRollover(previousSessionDate, nextSessionDate) {
    if (state.rolloverRefreshing) return;
    state.rolloverRefreshing = true;
    state.historyLoaded = false;
    state.historyError = '';
    try {
      await refreshPlanningLists();
      if (state.symbol) {
        await fetchPlanAndDrawings(state.symbol, state.selectionToken);
      }
      if (state.listMode === 'history') {
        await loadWatchlistHistory(state.historyRange.start, state.historyRange.end);
      }
      setGlobal(`NYSE session advanced ${previousSessionDate} → ${nextSessionDate}. Watchlist refreshed.`);
    } catch (error) {
      setGlobal(`NYSE session changed, but Watchlist refresh failed: ${error.message}`);
    } finally {
      state.rolloverRefreshing = false;
    }
  }

  function startStatusStripRefresh() {
    state.watchlistSessionDate = String(
      state.session?.market_status?.watchlist_session_date || state.watchlistSessionDate || ''
    ) || null;
    renderStatusStrip({
      ...state.session,
      browser_connection: 'CONNECTED',
      web_service: 'HEALTHY',
      executor_health: 'UNKNOWN',
    });
    refreshStatusStrip();
    if (state.statusTimer !== null) window.clearInterval(state.statusTimer);
    state.statusTimer = window.setInterval(refreshStatusStrip, 10_000);
    if (marketClockTimer !== null) window.clearInterval(marketClockTimer);
    marketClockTimer = window.setInterval(renderMarketCountdown, 1_000);
  }

  function createPane(containerId, rsContainerId, overlayId, timeframe) {
    const container = byId(containerId);
    const rsContainer = byId(rsContainerId);
    const overlay = byId(overlayId);
    const root = container.closest('.chart-pane');
    if (!window.LightweightCharts) {
      overlay.textContent = 'Chart library unavailable';
      return null;
    }
    const chartLayout = {
      background: {type: LightweightCharts.ColorType.Solid, color: '#081310'},
      textColor: '#8fa49c',
      fontFamily: 'Inter, system-ui, sans-serif',
      attributionLogo: false,
    };
    const chart = LightweightCharts.createChart(container, {
      autoSize: true,
      layout: chartLayout,
      grid: {vertLines: {color: '#12231f'}, horzLines: {color: '#12231f'}},
      crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
      leftPriceScale: {visible: false},
      rightPriceScale: {
        visible: true, borderVisible: true, borderColor: '#20362f', minimumWidth: 68,
        scaleMargins: {top: .08, bottom: .22},
      },
      timeScale: {
        borderColor: '#20362f', timeVisible: timeframe === '1H', secondsVisible: false,
        rightOffset: 0, minBarSpacing: 1, fixLeftEdge: true, fixRightEdge: false,
        rightBarStaysOnScroll: false, lockVisibleTimeRangeOnResize: true,
      },
      handleScroll: {mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true},
      handleScale: {
        axisPressedMouseMove: {time: true, price: true},
        axisDoubleClickReset: {time: true, price: true},
        mouseWheel: true, pinch: true,
      },
      kineticScroll: {mouse: false, touch: true},
    });
    chart.applyOptions({layout: chartLayout});
    const candles = chart.addCandlestickSeries({
      upColor: '#39c894', downColor: '#e95b68', borderVisible: false,
      wickUpColor: '#39c894', wickDownColor: '#e95b68',
    });
    const volume = chart.addHistogramSeries({
      priceFormat: {type: 'volume'}, priceScaleId: '',
      priceLineVisible: false, lastValueVisible: false,
    });
    volume.priceScale().applyOptions({scaleMargins: {top: .82, bottom: 0}});
    const ema10 = chart.addLineSeries({color: '#f5c96a', lineWidth: 1, priceLineVisible: false, lastValueVisible: false});
    const ema20 = chart.addLineSeries({color: '#56bde9', lineWidth: 1, priceLineVisible: false, lastValueVisible: false});
    const ema50 = chart.addLineSeries({color: '#aa82e8', lineWidth: 1, priceLineVisible: false, lastValueVisible: false});
    const percentFormatter = value => `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`;
    const relativeChartLayout = {
      background: {type: LightweightCharts.ColorType.Solid, color: '#0b1218'},
      textColor: '#95a29f',
      fontFamily: 'Inter, system-ui, sans-serif',
      attributionLogo: false,
    };
    const rsChart = LightweightCharts.createChart(rsContainer, {
      autoSize: true,
      layout: relativeChartLayout,
      grid: {vertLines: {color: '#1b2a2d'}, horzLines: {color: '#1b2a2d'}},
      crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
      leftPriceScale: {visible: false},
      rightPriceScale: {
        visible: true, borderVisible: true, borderColor: '#294039', minimumWidth: 68,
        scaleMargins: {top: .08, bottom: .08},
      },
      timeScale: {
        borderColor: '#294039', timeVisible: timeframe === '1H', secondsVisible: false,
        rightOffset: 0, minBarSpacing: 1, fixLeftEdge: true, fixRightEdge: false,
        rightBarStaysOnScroll: false, lockVisibleTimeRangeOnResize: true,
      },
      handleScroll: {mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true},
      handleScale: {
        axisPressedMouseMove: {time: true, price: true},
        axisDoubleClickReset: {time: true, price: true},
        mouseWheel: true, pinch: true,
      },
      kineticScroll: {mouse: false, touch: true},
    });
    rsChart.applyOptions({layout: relativeChartLayout});
    const rsBackground = rsChart.addHistogramSeries({
      priceFormat: {type: 'volume'}, priceScaleId: '',
      priceLineVisible: false, lastValueVisible: false,
    });
    rsBackground.priceScale().applyOptions({scaleMargins: {top: 0, bottom: 0}});
    const relativeStrength = rsChart.addLineSeries({
      title: 'Relative vs SPY', color: '#9ca3af', lineWidth: 2,
      priceLineVisible: false,
      priceFormat: {type: 'custom', minMove: .1, formatter: percentFormatter},
    });
    const relativeBaseline = relativeStrength.createPriceLine({
      price: 0, color: '#94a3b8', lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true, title: 'SPY baseline',
    });
    const relativeSma = rsChart.addLineSeries({
      title: 'Relative SMA 50', color: '#e5e7eb', lineWidth: 1,
      priceLineVisible: false,
      priceFormat: {type: 'custom', minMove: .1, formatter: percentFormatter},
    });
    const breakoutHandle = document.createElement('button');
    breakoutHandle.type = 'button';
    breakoutHandle.className = 'breakout-drag-handle';
    breakoutHandle.setAttribute('aria-label', 'Drag breakout price or tap to enter an exact price');
    breakoutHandle.hidden = true;
    const breakoutHandleLabel = document.createElement('span');
    breakoutHandle.appendChild(breakoutHandleLabel);
    container.appendChild(breakoutHandle);
    const drawingCanvas = document.createElement('canvas');
    drawingCanvas.className = 'drawing-overlay';
    drawingCanvas.setAttribute('aria-label', `${timeframe} line drawing layer`);
    drawingCanvas.hidden = true;
    container.appendChild(drawingCanvas);
    const crosshairGuide = document.createElement('div');
    crosshairGuide.className = 'pane-crosshair-guide';
    crosshairGuide.hidden = true;
    root.appendChild(crosshairGuide);
    const pane = {
      chart, candles, volume, ema10, ema20, ema50, rsChart, rsBackground,
      relativeStrength, relativeSma, relativeBaseline, root, overlay, timeframe, hasRelative: false,
      breakoutLine: null, breakoutHandle, breakoutHandleLabel, bundle: null,
      container, rsContainer, crosshairGuide,
      drawingCanvas, drawingContext: drawingCanvas.getContext('2d'), drawingPointer: null,
      priceByTime: new Map(), relativeByTime: new Map(),
      lastPrice: null, lastRelative: 0, syncingCrosshair: false,
    };
    applyCompactRelativeLabels(pane);
    wireBreakoutHandle(pane);
    wireDrawingOverlay(pane);
    if (window.ResizeObserver) {
      pane.breakoutResizeObserver = new ResizeObserver(() => {
        positionBreakoutHandle(pane);
        renderPaneDrawings(pane);
      });
      pane.breakoutResizeObserver.observe(container);
    }
    let syncingRange = false;
    chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      requestAnimationFrame(() => positionBreakoutHandle(pane));
      requestAnimationFrame(() => renderPaneDrawings(pane));
      if (!pane.hasRelative || syncingRange || !range) return;
      syncingRange = true;
      rsChart.timeScale().setVisibleLogicalRange(range);
      syncingRange = false;
    });
    rsChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      requestAnimationFrame(() => positionBreakoutHandle(pane));
      if (!pane.hasRelative || syncingRange || !range) return;
      syncingRange = true;
      chart.timeScale().setVisibleLogicalRange(range);
      syncingRange = false;
    });
    chart.subscribeCrosshairMove(param => {
      positionBreakoutHandle(pane);
      if (!state.drawMode) renderPaneDrawings(pane);
      positionPaneCrosshairGuide(pane, 'price', param);
      syncPaneCrosshair(pane, 'price', param);
    });
    rsChart.subscribeCrosshairMove(param => {
      positionPaneCrosshairGuide(pane, 'relative', param);
      syncPaneCrosshair(pane, 'relative', param);
    });
    chart.subscribeClick(param => chartClicked(pane, param));
    return pane;
  }

  function chartTimeKey(value) {
    if (typeof value === 'number') return `number:${value}`;
    if (typeof value === 'string') return `string:${value}`;
    if (value && typeof value === 'object') {
      return `day:${value.year}-${value.month}-${value.day}`;
    }
    return '';
  }

  function positionPaneCrosshairGuide(pane, source, param) {
    if (!pane?.hasRelative || !param?.point || !Number.isFinite(param.point.x)) {
      if (pane?.crosshairGuide) pane.crosshairGuide.hidden = true;
      return;
    }
    const sourceContainer = source === 'price' ? pane.container : pane.rsContainer;
    const rootRect = pane.root.getBoundingClientRect();
    const sourceRect = sourceContainer.getBoundingClientRect();
    const x = sourceRect.left - rootRect.left + param.point.x;
    pane.crosshairGuide.style.transform = `translateX(${Math.round(x)}px)`;
    pane.crosshairGuide.hidden = false;
  }

  function syncPaneCrosshair(pane, source, param) {
    if (!pane || pane.syncingCrosshair) return;
    const targetChart = source === 'price' ? pane.rsChart : pane.chart;
    if (!pane.hasRelative || !param?.time || !param?.point) {
      pane.syncingCrosshair = true;
      try {
        targetChart.clearCrosshairPosition();
      } finally {
        pane.syncingCrosshair = false;
      }
      return;
    }

    const targetSeries = source === 'price' ? pane.relativeStrength : pane.candles;
    const values = source === 'price' ? pane.relativeByTime : pane.priceByTime;
    const fallback = source === 'price' ? pane.lastRelative : pane.lastPrice;
    const exact = values.get(chartTimeKey(param.time));
    const price = Number.isFinite(exact) ? exact : fallback;
    if (!Number.isFinite(price)) return;

    pane.syncingCrosshair = true;
    try {
      targetChart.setCrosshairPosition(price, param.time, targetSeries);
    } finally {
      pane.syncingCrosshair = false;
    }
  }

  function applyPaneDisplayPreferences(pane) {
    if (!pane) return;
    const preferences = state.displayPreferences;
    pane.volume.applyOptions({visible: preferences.volume});
    pane.ema10.applyOptions({visible: preferences.ema10});
    pane.ema20.applyOptions({visible: preferences.ema20});
    pane.ema50.applyOptions({visible: preferences.ema50});
    pane.relativeStrength.applyOptions({visible: preferences.relativeStrength});
    pane.relativeSma.applyOptions({visible: preferences.relativeStrength});
    pane.rsBackground.applyOptions({visible: preferences.relativeStrength});
    const relativeData = pane.bundle?.indicators?.relative_strength || [];
    pane.hasRelative = Boolean(preferences.relativeStrength && relativeData.length);
    pane.root.classList.toggle('has-relative', pane.hasRelative);
    if (!pane.hasRelative) pane.crosshairGuide.hidden = true;
    if (pane.bundle) {
      pane.candles.setMarkers(preferences.earnings ? earningsMarkers(pane.bundle) : []);
      pane.chart.applyOptions({timeScale: {
        timeVisible: pane.bundle.timeframe === '1H', visible: !pane.hasRelative,
      }});
      pane.rsChart.applyOptions({timeScale: {
        timeVisible: pane.bundle.timeframe === '1H', visible: pane.hasRelative,
      }});
    }
    requestAnimationFrame(() => {
      positionBreakoutHandle(pane);
      renderPaneDrawings(pane);
    });
  }

  function applyCompactRelativeLabels(pane) {
    if (!pane) return;
    const labelsVisible = !compactLayout.matches;
    pane.relativeStrength.applyOptions({
      title: labelsVisible ? 'Relative vs SPY' : '',
      lastValueVisible: labelsVisible,
    });
    pane.relativeSma.applyOptions({
      title: labelsVisible ? 'Relative SMA 50' : '',
      lastValueVisible: labelsVisible,
    });
    pane.relativeBaseline.applyOptions({
      axisLabelVisible: labelsVisible,
      title: labelsVisible ? 'SPY baseline' : '',
    });
  }

  function chartTimeToIso(value) {
    if (typeof value === 'number') return new Date(value * 1000).toISOString();
    if (typeof value === 'string') return new Date(`${value}T00:00:00Z`).toISOString();
    if (value && typeof value === 'object') {
      return new Date(Date.UTC(value.year, value.month - 1, value.day)).toISOString();
    }
    return null;
  }

  function buildFutureWhitespace(bars, timeframe) {
    if (!bars?.length) return [];
    const sessionCount = 120;
    if (timeframe === '1D') {
      const lastDay = String(bars.at(-1).time).slice(0, 10);
      const cursor = new Date(`${lastDay}T00:00:00Z`);
      if (!Number.isFinite(cursor.getTime())) return [];
      const future = [];
      while (future.length < sessionCount) {
        cursor.setUTCDate(cursor.getUTCDate() + 1);
        if (cursor.getUTCDay() === 0 || cursor.getUTCDay() === 6) continue;
        future.push({time: cursor.toISOString().slice(0, 10)});
      }
      return future;
    }

    const times = bars
      .map(bar => typeof bar.time === 'number' ? bar.time : Math.floor(Date.parse(bar.time) / 1000))
      .filter(Number.isFinite);
    if (!times.length) return [];
    const secondsPerDay = 24 * 60 * 60;
    const offsetsByDay = new Map();
    times.forEach(time => {
      const day = Math.floor(time / secondsPerDay) * secondsPerDay;
      if (!offsetsByDay.has(day)) offsetsByDay.set(day, []);
      offsetsByDay.get(day).push(time - day);
    });
    const fullSessionSize = Math.max(...Array.from(offsetsByDay.values(), offsets => offsets.length));
    const fullSessionDay = Math.max(...Array.from(offsetsByDay.entries())
      .filter(([, offsets]) => offsets.length === fullSessionSize)
      .map(([day]) => day));
    const sessionOffsets = [...new Set(offsetsByDay.get(fullSessionDay))].sort((a, b) => a - b);
    const future = [];
    let currentDay = Math.floor(times.at(-1) / secondsPerDay) * secondsPerDay;
    let sessionsAdded = 0;
    while (sessionsAdded < sessionCount) {
      currentDay += secondsPerDay;
      const weekday = new Date(currentDay * 1000).getUTCDay();
      if (weekday === 0 || weekday === 6) continue;
      sessionOffsets.forEach(offset => future.push({time: currentDay + offset}));
      sessionsAdded += 1;
    }
    return future;
  }

  function earningsEventTone(event) {
    if (String(event.status || '').toUpperCase() === 'EXPECTED') return 'upcoming';
    const reported = Number(event.reported_eps);
    const estimated = Number(event.estimated_eps);
    const hasReportedComparison = event.reported_eps !== null && event.reported_eps !== undefined
      && event.estimated_eps !== null && event.estimated_eps !== undefined
      && Number.isFinite(reported) && Number.isFinite(estimated);
    if (hasReportedComparison) {
      if (reported < estimated) return 'negative';
      if (reported > estimated) return 'positive';
      return 'neutral';
    }
    const surprise = Number(event.eps_surprise_pct);
    if (event.eps_surprise_pct !== null && event.eps_surprise_pct !== undefined && Number.isFinite(surprise)) {
      if (surprise < 0) return 'negative';
      if (surprise > 0) return 'positive';
      return 'neutral';
    }
    const growth = String(event.growth_status || '').toUpperCase();
    if (['LOSS', 'NEGATIVE_NOT_MEANINGFUL'].includes(growth)) return 'negative';
    if (growth === 'TURNAROUND') return 'positive';
    const growthPercent = Number(event.eps_yoy_growth_pct);
    if (event.eps_yoy_growth_pct !== null && event.eps_yoy_growth_pct !== undefined && Number.isFinite(growthPercent)) {
      if (growthPercent < 0) return 'negative';
      if (growthPercent > 0) return 'positive';
    }
    return 'neutral';
  }

  function earningsToneColor(tone) {
    return {
      positive: '#089981',
      negative: '#f23645',
      neutral: '#787b86',
      upcoming: '#f59e0b',
    }[tone] || '#787b86';
  }

  function earningsMarkers(bundle) {
    const timeByDate = new Map();
    (bundle.bars || []).forEach(bar => {
      const date = typeof bar.time === 'number'
        ? new Date(bar.time * 1000).toISOString().slice(0, 10)
        : String(bar.time).slice(0, 10);
      if (!timeByDate.has(date)) timeByDate.set(date, bar.time);
    });
    return (bundle.earnings || []).flatMap(event => {
      const date = String(event.date || '').slice(0, 10);
      const time = timeByDate.get(date);
      if (time === undefined || String(event.status || '').toUpperCase() === 'EXPECTED') return [];
      const color = earningsToneColor(earningsEventTone(event));
      return [{time, position: 'aboveBar', color, shape: 'circle', text: 'E'}];
    });
  }

  function earningsLabel(bundle) {
    const events = bundle.earnings || [];
    const lastTime = bundle.bars?.at(-1)?.time;
    const lastDate = typeof lastTime === 'number'
      ? new Date(lastTime * 1000).toISOString().slice(0, 10)
      : String(lastTime || '').slice(0, 10);
    const upcoming = events
      .filter(event => String(event.date || '').slice(0, 10) > lastDate)
      .sort((a, b) => String(a.date).localeCompare(String(b.date)))[0];
    if (upcoming) {
      const timing = String(upcoming.timing || '').toUpperCase();
      return `Next E ${String(upcoming.date).slice(5, 10)}${timing && timing !== 'UNKNOWN' ? ` ${timing}` : ''}`;
    }
    const reported = earningsMarkers(bundle).length;
    return reported ? `Earnings ${reported}` : 'Earnings N/A';
  }

  function updateEarningsChip(bundle) {
    const chip = byId('earnings-chip');
    chip.textContent = earningsLabel(bundle);
    const events = bundle.earnings || [];
    const lastTime = bundle.bars?.at(-1)?.time;
    const lastDate = typeof lastTime === 'number'
      ? new Date(lastTime * 1000).toISOString().slice(0, 10)
      : String(lastTime || '').slice(0, 10);
    const upcoming = events.some(event => String(event.date || '').slice(0, 10) > lastDate);
    const latestReported = events
      .filter(event => String(event.status || '').toUpperCase() !== 'EXPECTED')
      .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')))[0];
    chip.dataset.tone = upcoming ? 'upcoming'
      : latestReported ? earningsEventTone(latestReported) : 'neutral';
    chip.hidden = !state.displayPreferences.earnings;
  }

  function metricNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function rememberHeaderMetrics(symbol, context = {}) {
    const remembered = {...(state.headerMetricsBySymbol.get(symbol) || {})};
    ['adr_20', 'return_1m', 'return_3m'].forEach(key => {
      const value = metricNumber(context[key]);
      if (value !== null) remembered[key] = value;
    });
    state.headerMetricsBySymbol.set(symbol, remembered);
    return remembered;
  }

  function updateHeaderMetricChips(symbol, context = {}) {
    const metrics = rememberHeaderMetrics(symbol, context);
    const setChip = (id, label, key, colored) => {
      const chip = byId(id);
      const value = metricNumber(metrics[key]);
      chip.textContent = value === null
        ? `${label}N/A`
        : `${label}${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
      if (colored) chip.dataset.tone = value === null ? 'neutral' : value >= 0 ? 'positive' : 'negative';
    };
    setChip('adr-chip', 'ADR', 'adr_20', false);
    setChip('growth-1m-chip', '1M', 'return_1m', true);
    setChip('growth-3m-chip', '3M', 'return_3m', true);
    applyHeaderDataVisibility();
  }

  function normalizeBreakoutPrice(value) {
    const price = Number(value);
    if (!Number.isFinite(price) || price <= 0) return null;
    return Math.max(.01, Math.round(price * 100) / 100);
  }

  function breakoutVisualPrice() {
    return normalizeBreakoutPrice(state.breakoutDraftPrice ?? state.plan?.breakout_price);
  }

  function closeBreakoutPricePopup() {
    const popup = byId('breakout-price-popup');
    if (popup) popup.hidden = true;
    state.breakoutFollowup = null;
  }

  function openBreakoutPricePopup(price = breakoutVisualPrice(), followup = null) {
    const normalized = normalizeBreakoutPrice(price);
    if ((!followup && (!state.breakoutMode || normalized === null)) || state.breakoutSaving) return;
    setBreakoutMode(false, false);
    const popup = byId('breakout-price-popup');
    const input = byId('breakout-price-popup-input');
    state.breakoutFollowup = followup;
    byId('breakout-price-title').textContent = followup
      ? `Set ${followup.symbol} breakout for ${followup.target === 'buy_today' ? 'Buy Today' : 'Buylist'}`
      : 'Set exact price';
    input.value = normalized === null ? '' : normalized.toFixed(2);
    input.setCustomValidity('');
    popup.hidden = false;
    requestAnimationFrame(() => {
      input.focus({preventScroll: true});
      input.select();
    });
  }

  function requestBreakoutForList(target) {
    setPanel(`Set a breakout price for ${state.symbol} before adding it to ${target === 'buy_today' ? 'Buy Today' : 'Buylist'}.`, 'error');
    openBreakoutPricePopup(null, {symbol: state.symbol, target});
  }

  function updateBreakoutModeUi() {
    const button = byId('place-breakout');
    const quickButton = byId('quick-plan-details');
    button.classList.toggle('active', state.breakoutMode);
    button.setAttribute('aria-pressed', String(state.breakoutMode));
    button.textContent = state.breakoutMode ? 'Click chart' : 'Place on chart';
    quickButton.classList.toggle('active', state.breakoutMode);
    quickButton.setAttribute('aria-pressed', String(state.breakoutMode));
    quickButton.textContent = state.breakoutMode ? 'Click chart' : 'Breakout';
    Object.values(state.panes).forEach(pane => {
      if (!pane) return;
      pane.root.classList.toggle('breakout-placement', state.breakoutMode);
      pane.breakoutHandle.disabled = !state.breakoutMode;
    });
  }

  function setBreakoutMode(active, announce = true) {
    state.breakoutMode = Boolean(active && state.symbol && !state.breakoutSaving);
    if (state.breakoutMode) {
      state.drawMode = false;
      state.drawAnchor = null;
      state.selectedDrawingId = null;
      state.drawingDrag = null;
      updateDrawingModeUi();
    }
    updateBreakoutModeUi();
    if (announce) {
      setPanel(state.breakoutMode
        ? 'Breakout: click the exact price on the chart.'
        : 'Breakout placement cancelled.');
    }
  }

  async function toggleBreakoutPlacement() {
    if (state.breakoutMode) {
      setBreakoutMode(false);
      return;
    }
    setBreakoutMode(true);
    if (state.breakoutMode) byId('planning-panel').classList.remove('open');
  }

  function breakoutPriceAtClientY(pane, clientY) {
    const rect = pane.breakoutHandle.parentElement.getBoundingClientRect();
    const coordinate = Math.max(0, Math.min(rect.height, clientY - rect.top));
    return normalizeBreakoutPrice(pane.candles.coordinateToPrice(coordinate));
  }

  function positionBreakoutHandle(pane) {
    if (!pane?.breakoutHandle) return;
    const price = breakoutVisualPrice();
    const coordinate = price === null ? null : pane.candles.priceToCoordinate(price);
    const height = pane.breakoutHandle.parentElement.clientHeight;
    if (!Number.isFinite(coordinate) || coordinate < 0 || coordinate > height) {
      pane.breakoutHandle.hidden = true;
      return;
    }
    pane.breakoutHandle.hidden = false;
    pane.breakoutHandle.style.top = `${coordinate}px`;
    pane.breakoutHandleLabel.textContent = `Drag or tap · ${price.toFixed(2)}`;
  }

  function updateBreakoutDraft(price) {
    const normalized = normalizeBreakoutPrice(price);
    if (normalized === null) return null;
    state.breakoutDraftPrice = normalized;
    byId('breakout-input').value = normalized.toFixed(2);
    renderBreakout();
    return normalized;
  }

  async function commitBreakoutPrice(price, successMessage) {
    const normalized = updateBreakoutDraft(price);
    if (normalized === null || state.breakoutSaving) return false;
    state.breakoutSaving = true;
    renderPlan();
    const pendingSave = planningCommand('set_breakout', normalized, successMessage);
    state.breakoutSaving = false;
    state.breakoutDraftPrice = null;
    renderPlan();
    return pendingSave;
  }

  function wireBreakoutHandle(pane) {
    const handle = pane.breakoutHandle;
    handle.addEventListener('pointerdown', event => {
      if (!state.breakoutMode || planningPending() || state.breakoutSaving) return;
      event.preventDefault();
      event.stopPropagation();
      state.breakoutDragging = {
        pane,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        moved: false,
        originalPrice: breakoutVisualPrice(),
      };
      handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener('pointermove', event => {
      const gesture = state.breakoutDragging;
      if (gesture?.pane !== pane || gesture.pointerId !== event.pointerId) return;
      event.preventDefault();
      if (!gesture.moved && Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) < 5) return;
      if (!gesture.moved) {
        gesture.moved = true;
        handle.classList.add('dragging');
        setPanel('Dragging breakout — release to save.');
      }
      updateBreakoutDraft(breakoutPriceAtClientY(pane, event.clientY));
    });
    const finish = (event, save) => {
      const gesture = state.breakoutDragging;
      if (gesture?.pane !== pane || gesture.pointerId !== event.pointerId) return;
      event.preventDefault();
      event.stopPropagation();
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      handle.classList.remove('dragging');
      state.breakoutDragging = null;
      const price = state.breakoutDraftPrice;
      if (save && gesture.moved && price !== null) {
        setBreakoutMode(false, false);
        commitBreakoutPrice(price, `Breakout moved to ${price.toFixed(2)} — saved locally.`);
      } else {
        state.breakoutDraftPrice = null;
        renderPlan();
        if (save && !gesture.moved) openBreakoutPricePopup(gesture.originalPrice);
      }
    };
    handle.addEventListener('pointerup', event => finish(event, true));
    handle.addEventListener('pointercancel', event => finish(event, false));
  }

  function updateDrawingModeUi() {
    const desktopButton = byId('draw-line');
    const mobileButton = byId('mobile-draw-line');
    desktopButton.classList.toggle('active', state.drawMode);
    desktopButton.setAttribute('aria-pressed', String(state.drawMode));
    desktopButton.textContent = state.drawMode ? 'Line Active' : 'Line Tool';
    mobileButton.classList.toggle('active', state.drawMode);
    mobileButton.setAttribute('aria-pressed', String(state.drawMode));
    mobileButton.setAttribute('aria-label', state.drawMode ? 'Line Tool active' : 'Line Tool');
    Object.values(state.panes).forEach(pane => {
      if (!pane) return;
      pane.root.classList.toggle('drawing-mode', state.drawMode);
      pane.drawingCanvas.style.pointerEvents = state.drawMode ? 'auto' : 'none';
      if (!state.drawMode) pane.drawingPointer = null;
      renderPaneDrawings(pane);
    });
    updateDrawingSelectionUi();
  }

  function selectedDrawing() {
    return state.drawings.find(item => item.id === state.selectedDrawingId && !item.deleted) || null;
  }

  function updateDrawingSelectionUi() {
    const actions = byId('mobile-drawing-actions');
    const drawing = selectedDrawing();
    actions.hidden = !(compactLayout.matches && state.drawMode && drawing);
    if (drawing) {
      actions.querySelector('span').textContent = 'Shared line selected';
    }
  }

  function setDrawingMode(active, announce = true) {
    state.drawMode = Boolean(active && state.symbol);
    state.drawAnchor = null;
    state.selectedDrawingId = null;
    state.drawingDrag = null;
    if (state.drawMode) {
      state.breakoutMode = false;
      updateBreakoutModeUi();
    }
    updateDrawingModeUi();
    if (announce) {
      setPanel(state.drawMode
        ? 'Line Tool: click a start point, then an end point. Drag a saved line or either endpoint to adjust it.'
        : 'Line Tool cancelled.');
    }
  }

  function cancelDrawingModeForOtherControl(event) {
    if (!state.drawMode || !(event.target instanceof Element)) return;
    if (event.target.closest('#draw-line, #mobile-draw-line, #mobile-drawing-actions')) return;
    const control = event.target.closest(
      'button, input, select, textarea, a[href], [role="tab"], [role="menuitem"], [role="option"]',
    );
    if (control) setDrawingMode(false, false);
  }

  function drawingEventPoint(pane, event) {
    const rect = pane.drawingCanvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(rect.width, event.clientX - rect.left)),
      y: Math.max(0, Math.min(rect.height, event.clientY - rect.top)),
    };
  }

  function drawingChartPoint(pane, event) {
    const point = drawingEventPoint(pane, event);
    const time = pane.chart.timeScale().coordinateToTime(point.x);
    const price = pane.candles.coordinateToPrice(point.y);
    const timestamp = chartTimeToIso(time);
    if (time == null || !timestamp || !Number.isFinite(Number(price)) || Number(price) <= 0) return null;
    return {time, timestamp, price: Number(price), x: point.x, y: point.y};
  }

  function drawingForRender(drawing) {
    if (state.drawingDrag?.drawingId === drawing.id && state.drawingDrag.preview) {
      return state.drawingDrag.preview;
    }
    return drawing;
  }

  function drawingTimeCoordinate(pane, iso) {
    const scale = pane.chart.timeScale();
    const displayed = displayDrawingTime(iso, pane.timeframe);
    const exact = scale.timeToCoordinate(displayed);
    if (Number.isFinite(exact) || pane.timeframe !== '1H') return exact;

    // A daily point is stored at midnight, but intraday charts have no
    // midnight bar. Align it with the first available bar in that session.
    const sessionDate = new Date(iso).toISOString().slice(0, 10);
    const sessionTime = [...(pane.bundle?.bars || []), ...(pane.futureWhitespace || [])]
      .map(item => item.time)
      .filter(time => typeof time === 'number')
      .find(time => new Date(time * 1000).toISOString().slice(0, 10) === sessionDate);
    return sessionTime === undefined ? null : scale.timeToCoordinate(sessionTime);
  }

  function drawingToScreen(pane, drawing) {
    const visible = drawingForRender(drawing);
    const x1 = drawingTimeCoordinate(pane, visible.start_date);
    const x2 = drawingTimeCoordinate(pane, visible.end_date);
    const y1 = pane.candles.priceToCoordinate(Number(visible.start_price));
    const y2 = pane.candles.priceToCoordinate(Number(visible.end_price));
    if (![x1, x2, y1, y2].every(Number.isFinite)) return null;
    return {x1, y1, x2, y2};
  }

  function renderPaneDrawings(pane) {
    if (!pane?.drawingCanvas || !pane.drawingContext) return;
    const hasSavedDrawings = state.drawings.some(item => !item.deleted);
    const overlayNeeded = state.drawMode || hasSavedDrawings;
    pane.root.classList.toggle('has-drawings', hasSavedDrawings);
    pane.drawingCanvas.hidden = !overlayNeeded;
    if (!overlayNeeded) {
      if (pane.drawingCanvas.width !== 1) pane.drawingCanvas.width = 1;
      if (pane.drawingCanvas.height !== 1) pane.drawingCanvas.height = 1;
      return;
    }
    const rect = pane.drawingCanvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
    const width = Math.max(1, Math.floor(rect.width * ratio));
    const height = Math.max(1, Math.floor(rect.height * ratio));
    if (pane.drawingCanvas.width !== width || pane.drawingCanvas.height !== height) {
      pane.drawingCanvas.width = width;
      pane.drawingCanvas.height = height;
    }
    const context = pane.drawingContext;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);
    state.drawings
      .filter(item => !item.deleted)
      .forEach(item => {
        const points = drawingToScreen(pane, item);
        if (!points) return;
        const selected = state.drawMode && state.selectedDrawingId === item.id;
        context.save();
        context.strokeStyle = selected ? '#f97316' : '#60a5fa';
        context.lineWidth = selected ? 3 : 2;
        context.beginPath();
        context.moveTo(points.x1, points.y1);
        context.lineTo(points.x2, points.y2);
        context.stroke();
        if (selected) {
          for (const point of [{x: points.x1, y: points.y1}, {x: points.x2, y: points.y2}]) {
            context.beginPath();
            context.arc(point.x, point.y, compactLayout.matches ? 7 : 5, 0, Math.PI * 2);
            context.fillStyle = '#0f172a';
            context.fill();
            context.strokeStyle = '#bfdbfe';
            context.lineWidth = 2;
            context.stroke();
          }
        }
        context.restore();
      });
    if (state.drawMode && state.drawAnchor?.timeframe === pane.timeframe && pane.drawingPointer) {
      const startX = pane.chart.timeScale().timeToCoordinate(state.drawAnchor.time);
      const startY = pane.candles.priceToCoordinate(state.drawAnchor.price);
      if (Number.isFinite(startX) && Number.isFinite(startY)) {
        context.save();
        context.strokeStyle = '#93c5fd';
        context.lineWidth = 2;
        context.setLineDash([5, 4]);
        context.beginPath();
        context.moveTo(startX, startY);
        context.lineTo(pane.drawingPointer.x, pane.drawingPointer.y);
        context.stroke();
        context.restore();
      }
    }
  }

  function pointDistanceToSegment(point, start, end) {
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    if (dx === 0 && dy === 0) return Math.hypot(point.x - start.x, point.y - start.y);
    const offset = ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy);
    const amount = Math.max(0, Math.min(1, offset));
    return Math.hypot(point.x - (start.x + amount * dx), point.y - (start.y + amount * dy));
  }

  function hitTestDrawing(pane, point) {
    let best = null;
    state.drawings
      .filter(item => !item.deleted)
      .forEach(drawing => {
        const screen = drawingToScreen(pane, drawing);
        if (!screen) return;
        const candidates = [
          {drawing, part: 'start', distance: Math.hypot(point.x - screen.x1, point.y - screen.y1)},
          {drawing, part: 'end', distance: Math.hypot(point.x - screen.x2, point.y - screen.y2)},
          {drawing, part: 'line', distance: pointDistanceToSegment(point, {x: screen.x1, y: screen.y1}, {x: screen.x2, y: screen.y2})},
        ];
        candidates.forEach(candidate => {
          const limit = candidate.part === 'line' ? (compactLayout.matches ? 14 : 10) : (compactLayout.matches ? 18 : 12);
          if (candidate.distance <= limit && (!best || candidate.distance < best.distance)) best = candidate;
        });
      });
    return best;
  }

  async function placeDrawingAnchor(pane, point) {
    if (!state.drawMode || state.drawingSaving) return;
    if (!state.drawAnchor || state.drawAnchor.timeframe !== pane.timeframe) {
      state.drawAnchor = {...point, timeframe: pane.timeframe};
      state.selectedDrawingId = null;
      pane.drawingPointer = {x: point.x, y: point.y};
      renderPaneDrawings(pane);
      updateDrawingSelectionUi();
      setPanel('Line Tool: choose the end point.');
      return;
    }
    const first = state.drawAnchor;
    state.drawAnchor = null;
    pane.drawingPointer = null;
    state.drawingSaving = true;
    setPanel('Saving drawing...');
    try {
      const result = await api('/api/v1/drawings', {
        method: 'POST',
        body: JSON.stringify({
          id: crypto.randomUUID(), symbol: state.symbol,
          start_date: first.timestamp, start_price: first.price,
          end_date: point.timestamp, end_price: point.price,
          timeframe: first.timeframe,
        }),
      });
      state.drawings.push(result.drawing);
      state.selectedDrawingId = result.drawing.id;
      renderDrawings();
      renderDrawingList();
      setPanel('SAVED LOCALLY - Line Tool remains active.', 'success');
    } catch (error) {
      setPanel(error.message, 'error');
    } finally {
      state.drawingSaving = false;
    }
  }

  function wireDrawingOverlay(pane) {
    const canvas = pane.drawingCanvas;
    canvas.addEventListener('pointerdown', event => {
      if (!state.drawMode || state.drawingSaving || event.button !== 0) return;
      const point = drawingEventPoint(pane, event);
      const hit = hitTestDrawing(pane, point);
      if (!hit) {
        state.selectedDrawingId = null;
        pane.drawingPointer = point;
        renderPaneDrawings(pane);
        updateDrawingSelectionUi();
        return;
      }
      const chartPoint = drawingChartPoint(pane, event);
      if (!chartPoint) return;
      event.preventDefault();
      event.stopPropagation();
      state.drawAnchor = null;
      state.selectedDrawingId = hit.drawing.id;
      state.drawingDrag = {
        pane, pointerId: event.pointerId, drawingId: hit.drawing.id, part: hit.part,
        original: {...hit.drawing}, preview: {...hit.drawing}, startPoint: chartPoint, moved: false,
      };
      canvas.setPointerCapture(event.pointerId);
      renderPaneDrawings(pane);
      updateDrawingSelectionUi();
    });
    canvas.addEventListener('pointermove', event => {
      if (!state.drawMode) return;
      const drag = state.drawingDrag;
      if (drag?.pane === pane && drag.pointerId === event.pointerId) {
        const point = drawingChartPoint(pane, event);
        if (!point) return;
        event.preventDefault();
        const next = {...drag.original};
        drag.moved = drag.moved || Math.hypot(point.x - drag.startPoint.x, point.y - drag.startPoint.y) > 2;
        if (drag.part === 'start') {
          next.start_date = point.timestamp;
          next.start_price = point.price;
        } else if (drag.part === 'end') {
          next.end_date = point.timestamp;
          next.end_price = point.price;
        } else {
          const delta = point.price - drag.startPoint.price;
          next.start_price = Number(drag.original.start_price) + delta;
          next.end_price = Number(drag.original.end_price) + delta;
        }
        drag.preview = next;
        renderPaneDrawings(pane);
        return;
      }
      if (state.drawAnchor?.timeframe === pane.timeframe) {
        pane.drawingPointer = drawingEventPoint(pane, event);
        renderPaneDrawings(pane);
      }
    });
    canvas.addEventListener('pointerup', event => {
      if (!state.drawMode) return;
      const drag = state.drawingDrag;
      if (drag?.pane === pane && drag.pointerId === event.pointerId) {
        event.preventDefault();
        event.stopPropagation();
        if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
        state.drawingDrag = null;
        if (drag.moved) persistDrawingUpdate(drag.original, drag.preview, 'Drawing moved - saved locally.');
        else renderPaneDrawings(pane);
        return;
      }
      const point = drawingChartPoint(pane, event);
      if (point) placeDrawingAnchor(pane, point);
    });
    canvas.addEventListener('pointercancel', event => {
      if (state.drawingDrag?.pane !== pane || state.drawingDrag.pointerId !== event.pointerId) return;
      state.drawingDrag = null;
      renderPaneDrawings(pane);
    });
  }

  async function chartClicked(pane, param) {
    if (!state.symbol) return;
    if (state.breakoutMode) {
      const price = normalizeBreakoutPrice(
        Number.isFinite(param?.point?.y) ? pane.candles.coordinateToPrice(param.point.y) : null
      );
      if (price === null) return;
      setBreakoutMode(false, false);
      await commitBreakoutPrice(price, `Breakout set to ${price.toFixed(2)} — saved locally.`);
    }
  }

  function displayDrawingTime(iso, timeframe) {
    const date = new Date(iso);
    return timeframe === '1D' ? date.toISOString().slice(0, 10) : Math.floor(date.getTime() / 1000);
  }

  function renderDrawings() {
    Object.values(state.panes).forEach(renderPaneDrawings);
  }

  function renderBreakout() {
    const price = breakoutVisualPrice();
    Object.values(state.panes).forEach(pane => {
      if (!pane) return;
      if (price !== null) {
        if (!pane.breakoutLine) pane.breakoutLine = pane.candles.createPriceLine({
          price, color: '#f5c96a', lineWidth: 2, lineStyle: 1,
          axisLabelVisible: true, title: '',
        });
        else pane.breakoutLine.applyOptions({price});
        requestAnimationFrame(() => positionBreakoutHandle(pane));
      } else {
        if (pane.breakoutLine) pane.candles.removePriceLine(pane.breakoutLine);
        pane.breakoutLine = null;
        pane.breakoutHandle.hidden = true;
      }
    });
  }

  function setMarketAlignmentExpanded(open) {
    state.alignmentOpen = Boolean(open);
    const overlay = byId('market-alignment-overlay');
    const toggle = byId('market-alignment-toggle');
    const details = byId('market-alignment-details');
    overlay.classList.toggle('details-open', state.alignmentOpen);
    toggle.setAttribute('aria-expanded', String(state.alignmentOpen));
    toggle.textContent = state.alignmentOpen ? 'Hide details \u25b4' : 'Details \u25be';
    details.hidden = !state.alignmentOpen;
  }

  function renderMarketAlignment(value) {
    const alignment = value && typeof value === 'object' ? value : {};
    state.marketAlignment = alignment;
    const overlay = byId('market-alignment-overlay');
    const states = alignment.states && typeof alignment.states === 'object' ? alignment.states : {};
    const hasScore = alignment.score !== null && alignment.score !== undefined && alignment.score !== '' && Number.isFinite(Number(alignment.score));
    byId('alignment-score').textContent = hasScore ? String(alignment.score) : '\u2014';
    byId('alignment-label').textContent = alignment.leadership_label || 'N/A';
    byId('alignment-context').textContent = `CONTEXT: ${alignment.context_label || 'UNKNOWN'}`;
    ['MKT', 'SEG', 'SEC', 'IND'].forEach(label => {
      const indicator = overlay.querySelector(`[data-alignment-state="${label}"]`);
      const rawState = String(states[label] || 'UNKNOWN').toUpperCase();
      const componentState = ['GREEN', 'YELLOW', 'RED'].includes(rawState) ? rawState : 'UNKNOWN';
      indicator.dataset.state = componentState;
      indicator.title = `${label}: ${componentState.charAt(0)}${componentState.slice(1).toLowerCase()}`;
      indicator.setAttribute('aria-label', indicator.title);
      indicator.querySelector('i').textContent = componentState === 'UNKNOWN' ? '\u25cb' : '\u25cf';
    });
    byId('alignment-stale').hidden = !alignment.stale;
    byId('alignment-provisional').hidden = !alignment.provisional;
    const details = byId('market-alignment-details');
    details.replaceChildren();
    const sections = Array.isArray(alignment.details) && alignment.details.length
      ? alignment.details
      : [{title: 'Data', rows: [['Leadership Score', 'N/A'], ['Market Context', 'UNKNOWN'], ['Status', 'No published EOD snapshot']]}];
    sections.forEach(sectionValue => {
      const section = document.createElement('section');
      section.className = 'alignment-section';
      const title = document.createElement('div');
      title.className = 'alignment-section-title';
      title.textContent = sectionValue?.title || 'Details';
      section.append(title);
      (Array.isArray(sectionValue?.rows) ? sectionValue.rows : []).forEach(rowValue => {
        if (!Array.isArray(rowValue) || rowValue.length < 2) return;
        const row = document.createElement('div');
        row.className = 'alignment-detail-row';
        const key = document.createElement('span');
        key.className = 'alignment-detail-key';
        key.textContent = String(rowValue[0] ?? '');
        const rowValueElement = document.createElement('span');
        rowValueElement.className = 'alignment-detail-value';
        rowValueElement.textContent = String(rowValue[1] ?? 'N/A');
        row.append(key, rowValueElement);
        section.append(row);
      });
      details.append(section);
    });
    setMarketAlignmentExpanded(state.alignmentOpen);
    overlay.dataset.ready = '1';
    overlay.hidden = !state.displayPreferences.marketAlignment;
    renderMarketPulsePage();
  }

  function applyBundle(pane, bundle, {fit = true} = {}) {
    if (!pane || !bundle) return;
    pane.timeframe = bundle.timeframe;
    pane.bundle = bundle;
    const bars = bundle.bars || [];
    const futureWhitespace = buildFutureWhitespace(bars, bundle.timeframe);
    pane.futureWhitespace = futureWhitespace;
    pane.candles.setData([...bars, ...futureWhitespace]);
    pane.volume.setData(bundle.volume || []);
    pane.ema10.setData(bundle.indicators?.ema10 || []);
    pane.ema20.setData(bundle.indicators?.ema20 || []);
    pane.ema50.setData(bundle.indicators?.ema50 || []);
    const relativeData = bundle.indicators?.relative_strength || [];
    const relativeItemsByTime = new Map(relativeData.map(item => [chartTimeKey(item.time), item]));
    pane.priceByTime = new Map(bars.map(item => [chartTimeKey(item.time), Number(item.close)]));
    pane.relativeByTime = new Map(relativeData.map(item => [chartTimeKey(item.time), Number(item.value)]));
    pane.lastPrice = Number(bars.at(-1)?.close);
    pane.lastRelative = Number(relativeData.at(-1)?.value);
    if (!Number.isFinite(pane.lastRelative)) pane.lastRelative = 0;
    const coveredRelativeData = bars.map(item => (
      relativeItemsByTime.get(chartTimeKey(item.time)) || {time: item.time}
    ));
    pane.relativeStrength.setData([...coveredRelativeData, ...futureWhitespace]);
    pane.relativeStrength.setMarkers(bundle.indicators?.rs_markers || []);
    pane.relativeSma.setData([...(bundle.indicators?.rs_sma50 || []), ...futureWhitespace]);
    pane.rsBackground.setData([...(bundle.indicators?.rs_regime || []), ...futureWhitespace]);
    applyPaneDisplayPreferences(pane);
    pane.overlay.hidden = true;
    const profile = bundle.profile || {};
    if (bundle.symbol === state.symbol) {
      byId('company-name').textContent = profile.company || bundle.symbol;
      updateHeaderMetricChips(bundle.symbol, bundle.context);
      updateEarningsChip(bundle);
      renderMarketAlignment(bundle.market_alignment);
      const coverage = bundle.coverage || {};
      const start = String(coverage.actual_start || 'UNAVAILABLE').slice(0, 10);
      const end = String(coverage.actual_end || 'UNAVAILABLE').slice(0, 10);
      const chartMetadata = `${coverage.source || 'UNAVAILABLE'} · ${coverage.bar_count ?? 0} bars · ${start} → ${end} · ${coverage.completeness || 'UNAVAILABLE'}`;
      const companyContext = byId('company-context');
      companyContext.textContent = `${profile.exchange || 'UNAVAILABLE'} · ${profile.sector || 'UNAVAILABLE'} · ${profile.industry || 'UNAVAILABLE'} · RS ${bundle.context?.relative_strength ?? 'UNAVAILABLE'} · ${chartMetadata}`;
      companyContext.title = `${chartMetadata} · ${coverage.adjustment_mode || 'UNAVAILABLE'} · ${coverage.session_policy || 'UNAVAILABLE'}`;
    }
    renderDrawings();
    renderBreakout();
    if (fit) resetChartRange(pane);
  }

  function resetChartRange(pane) {
    if (!pane?.bundle) return;
    const bars = pane.bundle.bars || [];
    if (!bars.length) return;
    const visibleRange = {
      from: bars[Math.max(0, bars.length - DEFAULT_VISIBLE_BARS)].time,
      to: bars.at(-1).time,
    };
    const anchorLatest = () => {
      pane.chart.priceScale('right').applyOptions({autoScale: true});
      pane.rsChart.priceScale('right').applyOptions({autoScale: true});
      pane.chart.timeScale().setVisibleRange(visibleRange);
      if (pane.hasRelative) pane.rsChart.timeScale().setVisibleRange(visibleRange);
    };
    anchorLatest();
    requestAnimationFrame(anchorLatest);
  }

  async function getBundle(symbol, timeframe) {
    const key = `${symbol}:${timeframe}`;
    if (state.bundleCache.has(key)) return state.bundleCache.get(key);
    if (state.bundleInflight.has(key)) return state.bundleInflight.get(key);
    const request = api(`/api/v1/charts/${encodeURIComponent(symbol)}/${timeframe}`)
      .then(bundle => {
        state.bundleCache.set(key, bundle);
        rememberHeaderMetrics(symbol, bundle.context);
        if (state.symbol === symbol) updateHeaderMetricChips(symbol, bundle.context);
        while (state.bundleCache.size > 36) state.bundleCache.delete(state.bundleCache.keys().next().value);
        return bundle;
      })
      .finally(() => state.bundleInflight.delete(key));
    state.bundleInflight.set(key, request);
    return request;
  }

  async function loadCharts(symbol, token) {
    const targets = state.split
      ? [[state.panes.primary, '1D', 'primary-label'], [state.panes.secondary, '1H', 'secondary-label']]
      : [[state.panes.primary, state.timeframe, 'primary-label']];
    targets.forEach(([pane, timeframe]) => {
      pane.timeframe = timeframe;
      updatePaneTimeframeToggle(pane === state.panes.primary ? 'primary-label' : 'secondary-label', timeframe);
      pane.overlay.hidden = false;
      pane.overlay.textContent = `Loading ${timeframe}…`;
    });
    setGlobal(`${symbol} · loading ${targets.map(target => target[1]).join(' + ')}`);
    const outcomes = await Promise.all(targets.map(async ([pane, timeframe, labelId]) => {
      try {
        const bundle = await getBundle(symbol, timeframe);
        if (token !== state.selectionToken) return false;
        pane.timeframe = timeframe;
        updatePaneTimeframeToggle(labelId, timeframe);
        applyBundle(pane, bundle);
        return true;
      } catch (error) {
        if (token !== state.selectionToken) return false;
        pane.overlay.hidden = false;
        pane.overlay.textContent = `${timeframe} UNAVAILABLE — ${error.message}`;
        return false;
      }
    }));
    if (token !== state.selectionToken) return false;
    if (outcomes.some(Boolean)) {
      setGlobal(`${symbol} · chart ready`);
      byId('data-status').textContent = state.session.data_mode === 'PC_MIRROR'
        ? 'PC'
        : compactStatusWord(state.session.data_mode, 'DEMO');
      byId('data-dot').classList.add('good');
    } else {
      setGlobal(`${symbol} · chart unavailable`);
    }
    return outcomes.some(Boolean);
  }

  async function fetchPlanAndDrawings(symbol, token) {
    const requestedPlanningEpoch = planningEpoch(symbol);
    try {
      const [planResult, drawingResult] = await Promise.all([
        api(`/api/v1/planning/${encodeURIComponent(symbol)}`),
        api(`/api/v1/drawings/${encodeURIComponent(symbol)}`),
      ]);
      if (token !== state.selectionToken) return;
      if (requestedPlanningEpoch === planningEpoch(symbol)) {
        state.plan = state.optimisticPlans.get(symbol) || planResult.card;
      }
      state.drawings = drawingResult.rows || [];
      renderPlan();
      renderDrawings();
      renderDrawingList();
    } catch (error) {
      if (token === state.selectionToken) setPanel(error.message, 'error');
    }
  }

  async function selectSymbol(symbol, navigationKind = 'symbol-select') {
    if (!symbol) return;
    const startedAt = performance.now();
    closeBreakoutPricePopup();
    state.symbol = symbol.toUpperCase();
    rememberListCursor();
    state.plan = state.optimisticPlans.get(state.symbol)
      || state.planningRows.find(row => row.symbol === state.symbol)
      || state.buyTodayRows.find(row => row.symbol === state.symbol)
      || null;
    setPanel('');
    const chartCacheHit = state.bundleCache.has(`${state.symbol}:${state.timeframe}`);
    state.planningBusy = planningPending(state.symbol);
    state.selectionToken += 1;
    const token = state.selectionToken;
    localStorage.setItem('quant-web-symbol', state.symbol);
    byId('active-symbol').textContent = state.symbol;
    byId('adr-chip').textContent = 'ADR...';
    byId('growth-1m-chip').textContent = '1M...';
    byId('growth-1m-chip').dataset.tone = 'neutral';
    byId('growth-3m-chip').textContent = '3M...';
    byId('growth-3m-chip').dataset.tone = 'neutral';
    byId('earnings-chip').textContent = 'Earnings ...';
    byId('earnings-chip').dataset.tone = 'neutral';
    byId('company-name').textContent = 'Loading profile…';
    byId('market-alignment-overlay').hidden = true;
    delete byId('market-alignment-overlay').dataset.ready;
    setMarketAlignmentExpanded(false);
    state.drawMode = false;
    state.drawAnchor = null;
    state.selectedDrawingId = null;
    state.drawingDrag = null;
    state.drawings = [];
    state.breakoutMode = false;
    state.breakoutDraftPrice = null;
    state.breakoutDragging = null;
    updateDrawingModeUi();
    renderDrawingList();
    Object.values(state.panes).forEach(pane => pane?.breakoutHandle.classList.remove('dragging'));
    updateBreakoutModeUi();
    updateActiveStockRow();
    renderPlan(true);
    const chartRequest = loadCharts(state.symbol, token);
    const contextRequest = fetchPlanAndDrawings(state.symbol, token);
    const [chartOutcome] = await Promise.allSettled([chartRequest]);
    await nextPaint();
    if (token === state.selectionToken) {
      recordClientTiming('symbol-navigation-chart-paint', startedAt, {
        symbol: state.symbol,
        timeframe: state.timeframe,
        navigation: navigationKind,
        cache_hit: chartCacheHit,
        chart_ready: chartOutcome.status === 'fulfilled' && chartOutcome.value === true,
      });
    }
    await Promise.allSettled([contextRequest]);
    schedulePrefetch(token);
  }

  function schedulePrefetch(token) {
    clearTimeout(state.prefetchTimer);
    state.prefetchTimer = setTimeout(() => {
      if (token !== state.selectionToken) return;
      const index = state.visibleRows.findIndex(row => row.symbol === state.symbol);
      const requests = [[state.symbol, state.timeframe === '1D' ? '1H' : '1D']];
      [index - 1, index + 1]
        .filter(i => i >= 0 && i < state.visibleRows.length)
        .forEach(i => requests.push([state.visibleRows[i].symbol, state.timeframe]));
      requests.forEach(([nextSymbol, timeframe]) => getBundle(nextSymbol, timeframe).catch(() => {}));
    }, 650);
  }

  function purchaseDisplay(row) {
    if (!row) return null;
    const quantity = Math.max(0, Number(row.broker_quantity) || 0);
    const remaining = Math.max(0, Number(row.entry_remaining_target_quantity) || 0);
    const status = quantity > 0 ? (remaining > 0 ? 'PARTIALLY_BOUGHT' : 'BOUGHT')
      : row.purchase_status === 'SOLD' ? 'SOLD'
      : (row.canonical_stage || row.board_status) === 'ENTRY_PENDING' ? 'ENTRY_PENDING' : '';
    const labels = {BOUGHT: 'Bought', PARTIALLY_BOUGHT: 'Partially bought', SOLD: 'Bought · sold', ENTRY_PENDING: 'Entry pending'};
    if (!labels[status]) return null;
    const average = Number(row.average_entry_price);
    const title = quantity > 0
      ? `${labels[status]} · ${quantity} ${quantity === 1 ? 'share' : 'shares'}${average > 0 ? ` at $${average.toFixed(average < 1 ? 4 : 2)} average` : ''}`
      : status === 'SOLD' ? 'Bought earlier this session; position is now closed' : 'Entry submitted; no confirmed shares bought yet';
    return {status, label: labels[status], title};
  }

  function renderPurchaseStatus() {
    const badge = byId('chart-purchase-status');
    const row = state.plan?.symbol === state.symbol ? state.plan
      : state.buyTodayRows.find(item => item.symbol === state.symbol);
    const purchase = purchaseDisplay(row);
    badge.hidden = !purchase;
    badge.textContent = purchase
      ? compactLayout.matches && purchase.status === 'PARTIALLY_BOUGHT' ? 'Part bought' : purchase.label
      : '';
    badge.dataset.status = purchase?.status || '';
    badge.title = purchase?.title || '';
    badge.setAttribute('aria-label', purchase?.title || '');
  }

  function renderListMetric(metric, row) {
    const purchase = PLANNING_LIST_MODES.has(state.listMode) && purchaseDisplay(row);
    metric.textContent = purchase?.label || (Number.isFinite(row.score) ? row.score.toFixed(1) : (row.stage || ''));
    if (purchase) {
      metric.dataset.purchaseStatus = purchase.status;
      metric.title = purchase.title;
    }
  }

  function stockRow(row, index) {
    const button = document.createElement('button');
    button.className = `stock-row${row.symbol === state.symbol ? ' active' : ''}`;
    button.dataset.symbol = row.symbol;
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', row.symbol === state.symbol ? 'true' : 'false');
    const rank = document.createElement('span');
    rank.className = 'stock-rank';
    rank.textContent = String(row.rank || index + 1).padStart(2, '0');
    const identity = document.createElement('span');
    const symbol = document.createElement('span');
    symbol.className = 'stock-symbol';
    symbol.textContent = row.symbol;
    const name = document.createElement('small');
    name.className = 'stock-name';
    name.textContent = row.name || row.company || row.stage || row.symbol;
    identity.append(symbol, name);
    const metric = document.createElement('span');
    metric.className = 'stock-metric';
    renderListMetric(metric, row);
    button.append(rank, identity, metric);
    button.addEventListener('click', () => selectSymbol(row.symbol));
    return button;
  }

  function renderStockList() {
    const list = byId('stock-list');
    state.stockRows.clear();
    if (compactLayout.matches) {
      list.replaceChildren();
      renderMobileStockList();
      return;
    }
    const fragment = document.createDocumentFragment();
    state.visibleRows.forEach((row, index) => {
      const button = stockRow(row, index);
      state.stockRows.set(row.symbol, button);
      fragment.appendChild(button);
    });
    if (!state.visibleRows.length) {
      const empty = document.createElement('div');
      empty.className = 'stock-list-empty';
      empty.textContent = listEmptyMessage();
      fragment.appendChild(empty);
    }
    replaceStockListRows(list, fragment);
    renderMobileStockList();
  }

  function replaceStockListRows(list, fragment) {
    const key = `${state.listMode}:${list.id}`;
    const scrollTop = state.listScrollPositions.get(key) || 0;
    list.replaceChildren(fragment);
    list.scrollTop = scrollTop;
  }

  function rememberListCursor() {
    const index = state.visibleRows.findIndex(row => row.symbol === state.symbol);
    state.listCursor = index < 0 || !PLANNING_LIST_MODES.has(state.listMode) ? null : {
      mode: state.listMode,
      symbol: state.symbol,
      index,
      symbols: state.visibleRows.map(row => row.symbol),
    };
  }

  function listContinuationSymbol(direction) {
    const cursor = state.listCursor;
    if (!cursor || cursor.mode !== state.listMode || cursor.symbol !== state.symbol) return null;
    const available = new Set(state.visibleRows.map(row => row.symbol));
    const after = cursor.symbols.slice(cursor.index + 1);
    const before = cursor.symbols.slice(0, cursor.index).reverse();
    const neighbors = direction < 0 ? [...before, ...after] : [...after, ...before];
    return neighbors.find(symbol => available.has(symbol))
      || state.visibleRows[Math.min(cursor.index, state.visibleRows.length - 1)]?.symbol;
  }

  function preservePlanningListOrder(mode, rows) {
    const order = state.listOrders.get(mode) || [];
    const known = new Set(order);
    rows.forEach(row => {
      if (!known.has(row.symbol)) {
        order.push(row.symbol);
        known.add(row.symbol);
      }
    });
    // Keep removed symbols' slots so a rejected optimistic edit restores its order.
    state.listOrders.set(mode, order);
    const current = new Map(rows.map(row => [row.symbol, row]));
    return order.filter(symbol => current.has(symbol))
      .map((symbol, index) => ({...current.get(symbol), rank: index + 1}));
  }

  function listEmptyMessage() {
    if (state.listMode !== 'history') return 'No stocks in this list.';
    if (state.historyLoading) return 'Loading watchlist history...';
    if (state.historyError) return state.historyError;
    return 'No stocks were in Watchlist during this range.';
  }

  function mobileStockRow(row, index) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `mobile-list-row${row.symbol === state.symbol ? ' active' : ''}`;
    button.dataset.mobileSymbol = row.symbol;
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(row.symbol === state.symbol));
    const rank = document.createElement('span');
    rank.className = 'mobile-list-rank';
    rank.textContent = String(row.rank || index + 1);
    const identity = document.createElement('span');
    identity.className = 'mobile-list-identity';
    const symbol = document.createElement('strong');
    symbol.textContent = row.symbol;
    const name = document.createElement('small');
    name.textContent = row.name || row.company || row.stage || row.symbol;
    identity.append(symbol, name);
    const metric = document.createElement('span');
    metric.className = 'mobile-list-metric';
    renderListMetric(metric, row);
    button.append(rank, identity, metric);
    button.addEventListener('click', () => {
      byId('mobile-list-popover').hidden = true;
      byId('mobile-list-menu').setAttribute('aria-expanded', 'false');
      selectSymbol(row.symbol);
    });
    return button;
  }

  function monitoringRows() {
    const cached = new Map(state.monitorRows.map(row => [row.symbol, row]));
    const members = new Map();
    state.planningRows.forEach(row => {
      if (row.watchlist_member || row.buylist_member || row.buy_today_member) members.set(row.symbol, {...row});
    });
    state.buyTodayRows.forEach(row => members.set(row.symbol, {...members.get(row.symbol), ...row, buy_today_member: true}));
    return [...members.values()].map(card => {
      const quote = cached.get(card.symbol) || {};
      const changedLevel = normalizeBreakoutPrice(quote.breakout_price) !== normalizeBreakoutPrice(card.breakout_price);
      const result = {...quote, ...card};
      const active = ['REGULAR', 'PRE_MARKET', 'AFTER_HOURS'].includes(state.session?.market_status?.phase);
      const age = quote.quote_as_of ? (Date.now() - new Date(quote.quote_as_of).getTime()) / 1000 : Infinity;
      const stale = Boolean(state.monitorError) || quote.quote_status === 'STALE' || (active && age > state.monitorStaleSeconds);
      if (stale) result.quote_status = quote.current_price ? 'STALE' : 'UNAVAILABLE';
      if (changedLevel) {
        result.breakout_status = normalizeBreakoutPrice(card.breakout_price) === null ? 'NO_LEVEL' : 'UNKNOWN';
        result.broke_out_today = null;
        result.orb = [];
      } else if (stale) {
        result.breakout_status = normalizeBreakoutPrice(card.breakout_price) === null ? 'NO_LEVEL' : 'UNKNOWN';
        result.broke_out_today = null;
        const sessionDay = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
        result.orb = quote.orb_session_date === sessionDay ? (quote.orb || []).map(orb =>
          orb.price_status === 'PASS' ? {...orb} : {...orb, price_status: 'UNKNOWN', price_reason: 'Quote is stale'}) : [];
      }
      return result;
    }).filter(row => {
      if (state.monitorQuery && !`${row.symbol} ${row.name || ''}`.toLowerCase().includes(state.monitorQuery)) return false;
      if (state.monitorFilter === 'breakouts') return row.broke_out_today === true;
      if (state.monitorFilter === 'orb') return (row.orb || []).some(orb => orb.price_status === 'PASS');
      if (state.monitorFilter === 'today') return Boolean(row.buy_today_member);
      return true;
    }).sort((a, b) => (
      Number(Boolean(b.broke_out_today)) - Number(Boolean(a.broke_out_today))
      || Number(Boolean(b.buy_today_member)) - Number(Boolean(a.buy_today_member))
      || a.symbol.localeCompare(b.symbol)
    ));
  }

  function monitorMarketClosed() {
    const phase = state.session?.market_status?.phase;
    return Boolean(phase && !['REGULAR', 'PRE_MARKET', 'AFTER_HOURS'].includes(phase));
  }

  function mobileMonitorRow(row) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `mobile-monitor-row${row.symbol === state.symbol ? ' active' : ''}`;
    button.dataset.mobileSymbol = row.symbol;
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(row.symbol === state.symbol));
    const add = (parent, tag, text, className = '') => {
      const node = document.createElement(tag);
      node.textContent = text;
      node.className = className;
      parent.appendChild(node);
      return node;
    };
    const top = add(button, 'span', '', 'mobile-monitor-top');
    add(top, 'strong', row.symbol);
    const labels = {ABOVE: 'Broken out', PULLED_BACK: 'Broke out · pulled back', WAITING: 'Waiting', NO_LEVEL: 'Set breakout', PRE_MARKET: 'Pre-market', CLOSED: 'Market closed', UNKNOWN: 'Awaiting data'};
    const status = add(top, 'small', labels[row.breakout_status] || 'Awaiting data', 'mobile-monitor-breakout');
    status.dataset.status = row.breakout_status || 'UNKNOWN';
    const prices = add(button, 'span', '', 'mobile-monitor-prices');
    const closed = monitorMarketClosed() || row.quote_status === 'CLOSED';
    const price = add(prices, 'span', closed ? 'Latest price' : 'Current price', 'mobile-monitor-price-block');
    const currentPrice = Number(row.current_price);
    add(price, 'b', Number.isFinite(currentPrice) && currentPrice > 0 ? currentPrice.toFixed(currentPrice < 1 ? 4 : 2) : '—');
    const level = add(prices, 'span', 'Breakout price', 'mobile-monitor-price-block');
    add(level, 'b', normalizeBreakoutPrice(row.breakout_price)?.toFixed(2) || 'Not set');
    const distance = normalizeBreakoutPrice(row.current_price) && normalizeBreakoutPrice(row.breakout_price)
      ? (row.current_price / row.breakout_price - 1) * 100 : null;
    const meta = add(button, 'span', '', 'mobile-monitor-meta');
    const quoted = row.quote_as_of ? new Date(row.quote_as_of) : null;
    const quoteDay = quoted?.toLocaleDateString('en-US', {timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric'});
    const today = new Date().toLocaleDateString('en-US', {timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric'});
    const quoteTime = quoted ? `${quoteDay === today ? '' : `${quoteDay} · `}${quoted.toLocaleTimeString('en-US', {timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false})}` : '';
    const sourceLabel = row.quote_source === 'DAILY_CLOSE' ? 'Last close' : closed ? 'Last price' : '';
    add(meta, 'small', !quoteTime ? state.monitorAsOf ? 'Quote unavailable' : 'Fetching latest price…'
      : `${sourceLabel ? `${sourceLabel} · ` : ''}${quoteTime} ET${row.quote_status === 'STALE' ? ' · stale' : ''}`, `mobile-monitor-quote ${row.quote_status === 'STALE' ? 'stale' : ''}`);
    add(meta, 'small', distance === null ? '' : `${Math.abs(distance).toFixed(2)}% ${distance > 0 ? 'above' : 'below'} breakout`, 'mobile-monitor-distance');
    if (!closed) {
      const table = add(button, 'span', '', 'mobile-monitor-orb');
      ['1m', '5m', '30m'].forEach(window => {
        const orb = (row.orb || []).find(item => item.window === window) || {};
        const line = add(table, 'span', '', 'mobile-monitor-orb-line');
        add(line, 'b', window);
        const cell = (value, reason) => {
          const names = {PASS: '✓ Passed', FAIL: '✕ Failed', WAITING: 'Await breakout', FORMING: 'Forming', UNKNOWN: 'Unavailable'};
          const node = add(line, 'span', names[value] || 'Unavailable', 'mobile-monitor-result');
          node.dataset.status = value || 'UNKNOWN';
          node.title = reason || '';
        };
        cell(orb.price_status, orb.price_reason);
        cell(orb.position_status, orb.position_reason);
        if (orb.price_status === 'WAITING') add(table, 'small', `${window}: Range complete · waiting above $${Number(orb.breakout_trigger || Math.max(row.breakout_price || 0, orb.high || 0)).toFixed(2)}`, 'mobile-monitor-reason');
        if (orb.price_status === 'UNKNOWN' && orb.price_reason) add(table, 'small', `${window}: ${orb.price_reason}`, 'mobile-monitor-reason');
        if (orb.price_status === 'PASS' && row.quote_status === 'STALE') add(table, 'small', `${window}: Passed earlier · latest quote stale`, 'mobile-monitor-reason');
        if (orb.position_status === 'FAIL' && orb.position_reason) add(table, 'small', `${window}: ${orb.position_reason}`, 'mobile-monitor-reason');
      });
    }
    button.addEventListener('click', () => {
      byId('mobile-list-popover').hidden = true;
      byId('mobile-list-menu').setAttribute('aria-expanded', 'false');
      void selectSymbol(row.symbol);
    });
    return button;
  }

  async function loadIntradayMonitor() {
    if (state.monitorLoading || !state.session || !compactLayout.matches || document.hidden) return;
    state.monitorLoading = true;
    if (state.monitorRetryTimer !== null) window.clearTimeout(state.monitorRetryTimer);
    state.monitorRetryTimer = null;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    try {
      const result = await api('/api/v1/intraday-monitor', {signal: controller.signal});
      state.monitorRows = result.rows || [];
      state.monitorAsOf = result.as_of;
      state.monitorEnabled = result.enabled;
      state.monitorStaleSeconds = Number(result.stale_seconds || 180);
      state.monitorError = result.error || '';
      state.monitorPositionError = result.position_context_error || '';
    } catch (error) {
      state.monitorError = error.name === 'AbortError'
        ? 'Price check timed out · retrying on the next check'
        : `Prices unavailable: ${error.message}`;
    } finally {
      window.clearTimeout(timeout);
      state.monitorLoading = false;
      if (state.listMode === 'monitor') applyListMode('monitor');
      renderBuyBoardPage();
      if (!byId('buy-board-action-sheet').hidden) renderBuyBoardActionSheet();
      // Poll only the shared snapshot while its first quote batch is loading.
      if (!state.monitorAsOf && state.monitorEnabled === true && !state.monitorError) {
        state.monitorRetryTimer = window.setTimeout(loadIntradayMonitor, 5_000);
      }
    }
  }

  function startIntradayMonitorPolling() {
    if (state.monitorTimer !== null) window.clearInterval(state.monitorTimer);
    void loadIntradayMonitor();
    state.monitorTimer = window.setInterval(() => loadIntradayMonitor(), 60_000);
  }

  function renderMobileStockList() {
    const list = byId('mobile-list-items');
    if (!compactLayout.matches) {
      list.replaceChildren();
      return;
    }
    const fragment = document.createDocumentFragment();
    state.visibleRows.forEach((row, index) => {
      fragment.appendChild(state.listMode === 'monitor' ? mobileMonitorRow(row) : mobileStockRow(row, index));
    });
    if (!state.visibleRows.length) {
      const empty = document.createElement('div');
      empty.className = 'mobile-list-empty';
      empty.textContent = listEmptyMessage();
      fragment.appendChild(empty);
    }
    replaceStockListRows(list, fragment);
    byId('mobile-list-count').textContent = String(state.visibleRows.length);
    const monitor = state.listMode === 'monitor';
    byId('mobile-monitor-controls').hidden = !monitor;
    byId('mobile-monitor-status').hidden = !monitor;
    byId('mobile-monitor-legend').hidden = monitorMarketClosed();
    byId('mobile-monitor-status').textContent = state.monitorError || (state.monitorEnabled === false
      ? 'Sandbox · intraday quotes are available in Connected mode'
      : state.monitorAsOf ? monitorMarketClosed()
        ? 'Market closed · showing latest available prices. ORB checks resume next session.'
        : `Yahoo 1m · checks every minute · quotes may be delayed${state.monitorPositionError ? ` · ${state.monitorPositionError}` : ''}`
      : 'Checking prices · Watchlist + Buylist + Buy Today');
  }

  function updateActiveStockRow() {
    const active = byId('stock-list').querySelector('.stock-row.active');
    if (active) {
      active.classList.remove('active');
      active.setAttribute('aria-selected', 'false');
    }
    const selected = state.stockRows.get(state.symbol);
    if (selected) {
      selected.classList.add('active');
      selected.setAttribute('aria-selected', 'true');
    }
    document.querySelectorAll('[data-mobile-symbol]').forEach(button => {
      const isSelected = button.dataset.mobileSymbol === state.symbol;
      button.classList.toggle('active', isSelected);
      button.setAttribute('aria-selected', String(isSelected));
    });
  }

  async function refreshPlanningLists() {
    const [planningResult, buyTodayResult] = await Promise.all([
      api('/api/v1/planning'),
      api('/api/v1/buy-today-drafts'),
    ]);
    state.planningRows = planningResult.rows || [];
    state.buyTodayRows = buyTodayResult.rows || [];
    state.optimisticPlans.forEach((card, symbol) => {
      state.planningRows = state.planningRows.filter(row => row.symbol !== symbol);
      if (card && (card.watchlist_member || card.buylist_member)) {
        state.planningRows.unshift({...card});
      }
    });
    state.optimisticBuyToday.forEach((row, symbol) => {
      state.buyTodayRows = state.buyTodayRows.filter(item => item.symbol !== symbol);
      if (row) state.buyTodayRows.unshift({...row});
    });
    const revision = planningResult.revision || buyTodayResult.revision;
    if (revision) state.planningRevision = String(revision);
    if (buyTodayResult.draft_revision) state.buyTodayDraftRevision = String(buyTodayResult.draft_revision);
    const selected = state.planningRows.find(row => row.symbol === state.symbol)
      || state.buyTodayRows.find(row => row.symbol === state.symbol);
    if (selected && !planningPending() && !operatorPending()
      && Number(selected.version || 0) >= Number(state.plan?.version || 0)) state.plan = selected;
    if (state.listMode !== 'scanner') applyListMode(state.listMode);
    renderPlan();
    renderMobileWorkspace();
    if (state.session) void loadIntradayMonitor();
  }

  function applyListMode(mode, render = true) {
    if (state.listMode !== mode) state.listCursor = null;
    state.listMode = mode;
    document.querySelectorAll('.list-tab').forEach(button => button.classList.toggle('active', button.dataset.list === mode));
    const listLabels = {monitor: 'Intraday watch', scanner: 'Scanner', history: 'History', watchlist: 'Watchlist', buylist: 'Buylist', buy_today: 'Buy Today'};
    const activeListLabel = listLabels[mode] || 'Lists';
    byId('mobile-list-label').textContent = 'Watchlist';
    byId('mobile-list-title').textContent = mode === 'history' ? 'Watchlist History' : mode === 'monitor' && monitorMarketClosed() ? 'Watchlist prices' : activeListLabel;
    byId('mobile-list-items').setAttribute('aria-label', `${activeListLabel} stocks`);
    document.querySelectorAll('[data-history-form]').forEach(form => {
      form.hidden = mode !== 'history';
    });
    document.querySelectorAll('[data-mobile-list]').forEach(button => {
      const active = button.dataset.mobileList === mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    if (mode === 'monitor') {
      state.visibleRows = monitoringRows();
    } else if (mode === 'scanner') {
      state.visibleRows = [...state.manualRows, ...state.scanner.filter(row => !state.manualRows.some(manual => manual.symbol === row.symbol))];
    } else if (mode === 'history') {
      const knownRows = new Map([...state.manualRows, ...state.scanner].map(row => [row.symbol, row]));
      state.visibleRows = state.historyRows.map((row, index) => {
        const known = knownRows.get(row.symbol) || {};
        return {
          ...known,
          ...row,
          rank: index + 1,
          name: known.name || known.company || row.name || 'Historical Watchlist',
          score: null,
          stage: String(row.last_seen_date || '').slice(5),
        };
      });
    } else if (mode === 'buy_today') {
      state.visibleRows = state.buyTodayRows.map((row, index) => ({...row, rank: index + 1, name: row.name || 'BUY TODAY'}));
    } else if (mode === 'watchlist') {
      state.visibleRows = state.planningRows.filter(row => row.watchlist_member !== false).map((row, index) => ({...row, rank: index + 1, name: row.name || 'WATCHLIST'}));
    } else {
      state.visibleRows = state.planningRows.filter(row => row.buylist_member === true || row.stage === 'BUYLIST').map((row, index) => ({...row, rank: index + 1, name: row.name || row.stage}));
    }
    if (PLANNING_LIST_MODES.has(mode)) {
      state.visibleRows = preservePlanningListOrder(mode, state.visibleRows);
    }
    if (render) renderStockList();
    if (state.visibleRows.some(row => row.symbol === state.symbol)) {
      rememberListCursor();
    } else if (!planningPending() && !operatorPending()) {
      const nextSymbol = listContinuationSymbol(1);
      if (nextSymbol) void selectSymbol(nextSymbol, 'list-continuation');
      else state.listCursor = null;
    }
  }

  function refreshActivePlanningList() {
    if (PLANNING_LIST_MODES.has(state.listMode)) applyListMode(state.listMode);
  }

  async function loadWatchlistHistory(startDate, endDate) {
    const datePattern = /^\d{4}-\d{2}-\d{2}$/;
    if (!datePattern.test(startDate) || !datePattern.test(endDate) || startDate > endDate) {
      state.historyError = 'Choose a valid From and To date.';
      state.historyRows = [];
      state.historyLoaded = false;
      if (state.listMode === 'history') applyListMode('history');
      return;
    }
    state.historyRange = {start: startDate, end: endDate};
    syncHistoryInputs();
    state.historyRequestToken += 1;
    const token = state.historyRequestToken;
    state.historyLoading = true;
    state.historyLoaded = false;
    state.historyError = '';
    state.historyRows = [];
    if (state.listMode === 'history') applyListMode('history');
    try {
      const result = await api(`/api/v1/planning-history?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`);
      if (token !== state.historyRequestToken) return;
      state.historyRows = result.rows || [];
      state.historyLoaded = true;
    } catch (error) {
      if (token !== state.historyRequestToken) return;
      state.historyError = `History unavailable: ${error.message}`;
      state.historyRows = [];
    } finally {
      if (token === state.historyRequestToken) {
        state.historyLoading = false;
        if (state.listMode === 'history') applyListMode('history');
      }
    }
  }

  function activateListMode(mode) {
    applyListMode(mode);
    if (mode === 'history' && !state.historyLoaded && !state.historyLoading) {
      loadWatchlistHistory(state.historyRange.start, state.historyRange.end);
    }
  }

  async function loadScanner(setup = state.scannerSetup) {
    if (state.scannerLoading) return;
    state.scannerLoading = true;
    renderMobileWorkspace();
    try {
      const query = setup ? `&setup=${encodeURIComponent(setup)}` : '';
      const result = await api(`/api/v1/scanner?limit=300${query}`);
      state.scanner = result.rows || [];
      state.scannerSetup = result.setup || setup || '';
      state.scannerSetups = Array.isArray(result.available_setups)
        ? result.available_setups : [];
      state.scannerTotal = Number(result.total_matches ?? state.scanner.length);
      byId('scanner-setup').textContent = state.scannerSetup || 'Stored scanner';
      byId('scanner-freshness').textContent = `${result.source || 'UNKNOWN'} · session ${result.snapshot_date || 'UNAVAILABLE'} · ${result.freshness || 'UNKNOWN'}`;
      if (state.listMode === 'scanner') applyListMode('scanner');
    } finally {
      state.scannerLoading = false;
      renderMobileWorkspace();
    }
  }

  function hasCurrentBuyTodayDraft() {
    return state.buyTodayRows.some(row => (
      row.symbol === state.symbol && row.card_version === state.plan?.version
      && !row.buy_today_display_member && !row.purchase_status
    ));
  }

  function selectedSymbolName(symbol = state.symbol) {
    const row = [
      state.plan,
      ...state.planningRows,
      ...state.scanner,
      ...state.manualRows,
    ].find(item => item?.symbol === symbol);
    return row?.name || row?.company || symbol;
  }

  function optimisticPlan(
    operation, breakoutPrice = null, source = state.plan, symbol = state.symbol
  ) {
    const card = source ? {...source} : {
      symbol,
      name: selectedSymbolName(symbol),
      version: 0,
      card_version: 0,
      canonical_stage: 'WATCHLIST',
      watchlist_member: false,
      buylist_member: false,
      buy_today_member: false,
      breakout_price: null,
    };
    card.symbol = symbol;
    card.name = card.name || selectedSymbolName(symbol);
    card.watchlist_member = Boolean(card.watchlist_member);
    card.buylist_member = Boolean(card.buylist_member);
    if (operation === 'add_watchlist') card.watchlist_member = true;
    if (operation === 'remove_watchlist') card.watchlist_member = false;
    if (operation === 'promote_buylist') {
      card.buylist_member = true;
      card.canonical_stage = 'BUYLIST';
    }
    if (operation === 'remove_buylist') {
      card.buylist_member = false;
      card.canonical_stage = 'WATCHLIST';
    }
    if (operation === 'move_watchlist') {
      card.watchlist_member = true;
      card.buylist_member = false;
      card.canonical_stage = 'WATCHLIST';
    }
    if (operation === 'set_breakout') {
      card.breakout_price = normalizeBreakoutPrice(breakoutPrice);
      card.canonical_stage = card.canonical_stage || 'WATCHLIST';
    }
    if (operation === 'clear_breakout') {
      card.breakout_price = null;
      card.buylist_member = false;
      if (['BUYLIST', 'BUY_TODAY'].includes(card.canonical_stage)) {
        card.canonical_stage = 'WATCHLIST';
      }
    }
    card.stage = card.buylist_member
      ? 'BUYLIST'
      : card.watchlist_member
      ? 'WATCHLIST'
      : normalizeBreakoutPrice(card.breakout_price) !== null
      ? 'BREAKOUT'
      : 'NOT PLANNED';
    card.display_stage = !['WATCHLIST', 'BUYLIST'].includes(card.canonical_stage)
      ? card.canonical_stage
      : card.stage;
    card._optimistic = true;
    card._optimisticOperation = operation;
    return card;
  }

  function syncOptimisticPlanningRow(card = state.plan, symbol = state.symbol) {
    state.planningRows = state.planningRows.filter(row => row.symbol !== symbol);
    if (card && (card.watchlist_member || card.buylist_member)) {
      state.planningRows.unshift({...card});
    }
    if (state.listMode === 'watchlist' || state.listMode === 'buylist') {
      const popover = byId('mobile-list-popover');
      const listVisible = (!compactLayout.matches && !standaloneLayout.matches)
        || Boolean(popover && !popover.hidden);
      applyListMode(state.listMode, listVisible);
    }
  }

  function setOptimisticBuyToday(enabled, card = state.plan, symbol = state.symbol) {
    state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== symbol);
    if (enabled && card) {
      const row = {
        symbol,
        name: card.name || selectedSymbolName(symbol),
        breakout_price: card.breakout_price,
        card_version: card.version,
        _optimistic: true,
      };
      state.buyTodayRows.unshift(row);
      state.optimisticBuyToday.set(symbol, row);
    } else {
      state.optimisticBuyToday.set(symbol, null);
    }
    if (state.listMode === 'buy_today') applyListMode('buy_today');
  }

  function renderPlan(fast = false) {
    renderPurchaseStatus();
    const card = state.plan;
    const busy = planningPending() || operatorPending();
    const hasBuyTodayDraft = hasCurrentBuyTodayDraft();
    const canonicalBuyToday = canonicalBuyTodayActive(card);
    const hasBuyToday = canonicalBuyToday || hasBuyTodayDraft;
    const connectedReadOnly = state.session?.mode === 'CONNECTED' && !state.session?.planning_writable;
    const watchlistMember = card?.watchlist_member ?? (card?.stage === 'WATCHLIST' || card?.stage === 'BUYLIST');
    const buylistMember = card?.buylist_member ?? (card?.stage === 'BUYLIST');
    const buyTodayDraftEligible = Boolean(
      buylistMember && (card?.canonical_stage || card?.stage) === 'BUYLIST'
    );
    const operatorOperations = new Set(state.session?.operator?.operations || []);
    const operatorBuyTodayEnabled = Boolean(
      state.session?.operator?.delegated
      && operatorOperations.has(canonicalBuyToday ? 'deactivate_buy_today' : 'activate_buy_today')
    );
    const buyTodayActionEligible = hasBuyToday || buyTodayDraftEligible || (
      !connectedReadOnly && (card?.canonical_stage || card?.stage) === 'WATCHLIST'
    ) || (
      (card?.canonical_stage || card?.stage) === 'CLOSED'
      && Boolean(card?.breakout_price) && operatorBuyTodayEnabled
    );
    const displayStage = card?.display_stage || card?.canonical_stage || card?.stage || 'NOT PLANNED';
    byId('plan-stage').textContent = `${displayStage}${card?.is_ep ? ' · EP' : ''}`;
    byId('chart-entry-profile').hidden = !card?.is_ep;
    byId('plan-revision').textContent = operatorPending()
      ? `Revision ${card?.version || 0} · Queued`
      : busy || state.breakoutSaving
      ? `Revision ${card?.version || 0} · Saving`
      : `Revision ${card?.version || 0}`;
    const displayedBreakout = breakoutVisualPrice();
    byId('breakout-input').value = displayedBreakout === null ? '' : displayedBreakout.toFixed(2);
    byId('add-watchlist').disabled = connectedReadOnly || watchlistMember;
    byId('promote-buylist').disabled = connectedReadOnly || !card?.breakout_price || buylistMember;
    byId('move-watchlist').disabled = connectedReadOnly || !buylistMember;
    byId('remove-watchlist').disabled = connectedReadOnly || !watchlistMember;
    byId('save-breakout').disabled = connectedReadOnly || !state.symbol || busy || state.breakoutSaving;
    byId('clear-breakout').disabled = connectedReadOnly || !card || !card.breakout_price || busy || state.breakoutSaving;
    byId('place-breakout').disabled = connectedReadOnly || !state.symbol || busy || state.breakoutSaving;
    const previewButton = byId('buy-today-preview');
    previewButton.disabled = busy || !card || !buyTodayActionEligible || Boolean(card?.entry_cancellation_pending);
    previewButton.textContent = hasBuyToday
      ? 'Remove from Buy Today'
      : operatorBuyTodayEnabled ? 'Publish to Buy Today' : 'Create Buy Today draft';
    previewButton.classList.toggle('cancel', hasBuyToday);
    byId('quick-stage').textContent = hasBuyToday ? 'BUY TODAY' : (displayStage === 'NOT PLANNED' ? 'NOT LISTED' : displayStage);
    const quickWatchlist = byId('quick-watchlist');
    const quickBuylist = byId('quick-buylist');
    quickWatchlist.disabled = connectedReadOnly || busy;
    quickBuylist.disabled = connectedReadOnly || busy;
    quickWatchlist.setAttribute('aria-pressed', String(watchlistMember));
    quickBuylist.setAttribute('aria-pressed', String(buylistMember));
    quickWatchlist.title = watchlistMember
      ? 'Remove from Watchlist; breakout and Buylist remain separate'
      : 'Add to Watchlist';
    quickBuylist.title = buylistMember
      ? 'Remove from Buylist and keep in Watchlist'
      : card?.breakout_price
      ? 'Add to Buylist'
      : 'Set a breakout price before adding to Buylist';
    const quickBuyToday = byId('quick-buy-today');
    quickBuyToday.disabled = busy || !buyTodayActionEligible || Boolean(card?.entry_cancellation_pending);
    quickBuyToday.textContent = card?.entry_cancellation_pending
      ? 'Cancelling…' : hasBuyToday ? 'Cancel Today' : 'Buy Today';
    quickBuyToday.title = hasBuyToday
      ? canonicalBuyToday ? 'Cancel the remaining buy intent; confirmed fills are preserved' : 'Cancel the shared Buy Today draft'
      : operatorBuyTodayEnabled ? 'Publish executable Buy Today intent after confirmation' : 'Create a shared, non-executable Buy Today draft';
    byId('buy-today-safety').textContent = operatorBuyTodayEnabled
      ? 'Confirmed Buy Today intent is written through verified Operator Control. The browser never places an order.'
      : 'This is a shared, non-executable planning draft. The browser never places an order.';
    quickBuyToday.classList.toggle('cancel', hasBuyToday);
    byId('quick-plan-details').disabled = connectedReadOnly || !state.symbol || busy || state.breakoutSaving;
    byId('quick-plan-details').title = 'Click, then choose the breakout price on the chart';
    byId('quick-watchlist').classList.toggle('current', watchlistMember);
    byId('quick-buylist').classList.toggle('current', buylistMember);
    byId('quick-buy-today').classList.toggle('current', hasBuyToday);
    const optimisticOperation = String(card?._optimisticOperation || '');
    quickWatchlist.classList.toggle('pending', ['add_watchlist', 'remove_watchlist'].includes(optimisticOperation));
    quickBuylist.classList.toggle('pending', ['promote_buylist', 'remove_buylist', 'move_watchlist'].includes(optimisticOperation));
    quickBuyToday.classList.toggle('pending', ['activate_buy_today', 'deactivate_buy_today', 'buy_today_draft'].includes(optimisticOperation));
    if (fast) return;
    updateBreakoutModeUi();
    renderBreakout();
    renderMobileWorkspace();
  }

  function commandId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-0000-4000-8000-${Math.random().toString(16).slice(2).padEnd(12, '0').slice(0, 12)}`;
  }

  async function sendPlanningCommand(symbol, operation, breakoutPrice, expectedRevision, previousPlan = null) {
    let revision = expectedRevision;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        return await api(`/api/v1/planning/${encodeURIComponent(symbol)}/commands`, {
          method: 'POST',
          body: JSON.stringify({
            command_id: commandId(), operation,
            expected_revision: revision,
            breakout_price: breakoutPrice,
          }),
        });
      } catch (error) {
        const current = error.current;
        const observationConflict = operation === 'set_breakout'
          && error.status === 409 && error.message.includes('Stale planning revision')
          && current?.symbol === symbol && Number(current.version) > Number(revision)
          && current.breakout_price === previousPlan?.breakout_price
          && (current.canonical_stage || current.stage) === (previousPlan?.canonical_stage || previousPlan?.stage);
        if (!observationConflict || attempt === 3) throw error;
        revision = current.version;
      }
    }
  }

  async function planningCommand(operation, breakoutPrice = null, successMessage = '') {
    if (!state.symbol || planningPending()) return false;
    const actionSymbol = state.symbol;
    const previousPlan = state.plan ? {...state.plan} : null;
    const expectedRevision = previousPlan?.version || 0;
    const optimistic = optimisticPlan(
      operation, breakoutPrice, previousPlan, actionSymbol
    );
    state.planningPendingSymbols.add(actionSymbol);
    bumpPlanningEpoch(actionSymbol);
    state.optimisticPlans.set(actionSymbol, optimistic);
    state.planningBusy = planningPending();
    state.plan = optimistic;
    syncOptimisticPlanningRow(optimistic, actionSymbol);
    renderPlan(true);
    setPanel('Saving to canonical store…');
    try {
      const result = await sendPlanningCommand(
        actionSymbol, operation, breakoutPrice, expectedRevision, previousPlan
      );
      state.optimisticPlans.delete(actionSymbol);
      syncOptimisticPlanningRow(result.card, actionSymbol);
      if (state.symbol === actionSymbol) {
        state.plan = result.card;
        setPanel(successMessage || `${result.status} — confirmed.`, 'success');
      }
      void refreshPlanningLists().catch(() => {});
      return true;
    } catch (error) {
      state.optimisticPlans.delete(actionSymbol);
      const restored = error.status === 409 && error.current
        ? (Object.keys(error.current).length ? error.current : null)
        : previousPlan;
      syncOptimisticPlanningRow(restored, actionSymbol);
      if (state.symbol === actionSymbol) {
        state.plan = restored;
        setPanel(
          error.status === 409
            ? `CONFLICT — ${error.message}. Current state reloaded.`
            : `UNAVAILABLE — ${error.message}`,
          'error',
        );
      }
      return false;
    } finally {
      bumpPlanningEpoch(actionSymbol);
      state.planningPendingSymbols.delete(actionSymbol);
      state.planningBusy = planningPending();
      refreshActivePlanningList();
      if (state.symbol === actionSymbol) renderPlan();
    }
  }

  async function saveBuyTodayDraft(symbol, expectedRevision) {
    await api(`/api/v1/planning/${encodeURIComponent(symbol)}/buy-today-preview`, {
      method: 'POST', body: JSON.stringify({expected_revision: expectedRevision}),
    });
  }

  async function cancelBuyTodayDraft(symbol, expectedRevision) {
    await api(`/api/v1/planning/${encodeURIComponent(symbol)}/buy-today-preview`, {
      method: 'DELETE', body: JSON.stringify({expected_revision: expectedRevision}),
    });
  }

  function canonicalBuyTodayActive(card = state.plan) {
    return Boolean(
      card?.buy_today_member
      || ['BUY_TODAY', 'ENTRY_PENDING'].includes(card?.canonical_stage || card?.stage)
      || ((card?.canonical_stage || card?.stage) === 'OPEN_POSITION' && Number(card?.entry_remaining_target_quantity) > 0)
    );
  }

  function operatorOperationEnabled(operation) {
    return Boolean(
      state.session?.operator?.delegated
      && (state.session?.operator?.operations || []).includes(operation)
    );
  }

  function optimisticBuyTodayPlan(source, enabled, symbol) {
    const card = source ? {...source} : null;
    if (!card) return null;
    card.symbol = symbol;
    card.buy_today_member = Boolean(enabled);
    card.buylist_member = true;
    card.canonical_stage = enabled ? 'BUY_TODAY' : 'BUYLIST';
    card.stage = 'BUYLIST';
    card.display_stage = enabled ? 'BUY_TODAY' : 'BUYLIST';
    card._optimistic = true;
    card._optimisticOperation = enabled ? 'activate_buy_today' : 'deactivate_buy_today';
    return card;
  }

  async function reconcileOperatorCommand(symbol, commandId) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 2_000));
      let command;
      try {
        command = await api(`/api/v1/operator/commands/${encodeURIComponent(commandId)}`);
      } catch (_error) {
        continue;
      }
      if (!command.terminal) continue;
      state.operatorPendingCommands.delete(symbol);
      state.optimisticPlans.delete(symbol);
      state.optimisticBuyToday.delete(symbol);
      try {
        const [planResult] = await Promise.all([
          api(`/api/v1/planning/${encodeURIComponent(symbol)}`),
          refreshPlanningLists(),
        ]);
        syncOptimisticPlanningRow(planResult.card, symbol);
        if (state.symbol === symbol) {
          state.plan = planResult.card;
          setPanel(
            command.success
              ? 'Operator command confirmed by the Execution Owner.'
              : `Operator command ${command.status.toLowerCase()}: ${command.error || 'no change was committed.'}`,
            command.success ? 'success' : 'error',
          );
          renderPlan();
        }
      } catch (error) {
        if (state.symbol === symbol) setPanel(`Refresh unavailable — ${error.message}`, 'error');
      }
      return;
    }
    if (state.symbol === symbol) {
      setPanel('Operator request is still queued for the Execution Owner. It remains visible on the Buy Board.', 'error');
      renderPlan();
    }
  }

  async function toggleCanonicalBuyToday() {
    if (!state.symbol || planningPending() || operatorPending() || !state.plan) return;
    const actionSymbol = state.symbol;
    const previousPlan = {...state.plan};
    const previousBuyTodayRow = state.buyTodayRows.find(row => row.symbol === actionSymbol);
    const enabled = !canonicalBuyTodayActive(previousPlan);
    let isEp = false;
    const operation = enabled ? 'activate_buy_today' : 'deactivate_buy_today';
    if (!operatorOperationEnabled(operation)) {
      setPanel('Mobile operator control is not available for this action.', 'error');
      return;
    }
    if (enabled) {
      const reentry = (previousPlan.canonical_stage || previousPlan.stage) === 'CLOSED';
      const confirmed = await confirmBuyTodayPublication(
        actionSymbol,
        reentry
          ? `Re-enter ${actionSymbol}: This starts a new trade cycle after a fully reconciled exit. The Execution Owner may buy again when current ORB, fresh market data, risk, capital and broker checks pass. It does not automatically repeat after another stop.`
          : 'This creates executable intent on the shared Buy Board. It does not place an order now, but the Execution Owner may act later when every runtime, risk, market-data, and broker gate passes.',
      );
      if (!confirmed) return;
      isEp = confirmed === 'ep';
    }
    const optimistic = optimisticBuyTodayPlan(previousPlan, enabled, actionSymbol);
    if (enabled) optimistic.is_ep = isEp;
    const requestId = commandId();
    state.planningPendingSymbols.add(actionSymbol);
    bumpPlanningEpoch(actionSymbol);
    state.optimisticPlans.set(actionSymbol, optimistic);
    state.plan = optimistic;
    syncOptimisticPlanningRow(optimistic, actionSymbol);
    setOptimisticBuyToday(enabled, optimistic, actionSymbol);
    renderPlan(true);
    setPanel(enabled ? 'Publishing Buy Today intent…' : 'Removing from Buy Today…');
    let queued = false;
    try {
      const result = await api(`/api/v1/planning/${encodeURIComponent(actionSymbol)}/activate-buy-today`, {
        method: 'POST',
        body: JSON.stringify({
          command_id: requestId,
          expected_revision: previousPlan.version,
          enabled,
          is_ep: isEp,
        }),
      });
      queued = Boolean(result.queued);
      if (queued) {
        state.operatorPendingCommands.set(actionSymbol, requestId);
        setPanel(`${result.status}. The chart can be used while the backend confirms it.`, 'success');
        void reconcileOperatorCommand(actionSymbol, requestId);
      } else {
        state.optimisticPlans.delete(actionSymbol);
        state.optimisticBuyToday.delete(actionSymbol);
        syncOptimisticPlanningRow(result.card, actionSymbol);
        if (state.symbol === actionSymbol) {
          state.plan = result.card;
          setPanel(`${result.status}. No broker order was placed.`, 'success');
        }
        void refreshPlanningLists().catch(() => {});
      }
    } catch (error) {
      state.optimisticPlans.delete(actionSymbol);
      state.optimisticBuyToday.delete(actionSymbol);
      syncOptimisticPlanningRow(previousPlan, actionSymbol);
      state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== actionSymbol);
      if (previousBuyTodayRow) state.buyTodayRows.push(previousBuyTodayRow);
      if (state.listMode === 'buy_today') applyListMode('buy_today');
      if (state.symbol === actionSymbol) {
        state.plan = error.status === 409 && error.current
          ? (Object.keys(error.current).length ? error.current : null)
          : previousPlan;
        setPanel(`UNAVAILABLE — ${error.message}`, 'error');
      }
    } finally {
      bumpPlanningEpoch(actionSymbol);
      state.planningPendingSymbols.delete(actionSymbol);
      state.planningBusy = planningPending();
      refreshActivePlanningList();
      if (state.symbol === actionSymbol) renderPlan();
      if (!queued) renderMobileWorkspace();
    }
  }

  async function publishTodayPlan() {
    const operator = state.session?.operator || {};
    if (!operator.delegated || !(operator.operations || []).includes('publish_today_plan')) {
      byId('publish-today-status').textContent = operator.reason || 'Operator Control is unavailable.';
      byId('publish-today-status').className = 'mobile-inline-status error';
      return;
    }
    const confirmed = await confirmOperatorAction(
      "Publish Today's Plan?",
      'This publishes the complete pre-market planning snapshot for the Execution Owner. It does not place an order. Publication is rejected during regular market hours, on stale revisions, while an operator command is active, or when the Buy Today execution queue does not match canonical breakout targets.',
      "Publish Today's Plan",
    );
    if (!confirmed) return;
    const button = byId('publish-today-plan');
    const status = byId('publish-today-status');
    button.disabled = true;
    button.textContent = 'Publishing…';
    status.textContent = 'Verifying authority, queue targets, revisions, and read-back…';
    status.className = 'mobile-inline-status';
    try {
      const result = await api('/api/v1/operator/publish-today-plan', {
        method: 'POST',
        body: JSON.stringify({command_id: commandId()}),
      });
      status.textContent = result.execution_owner_heartbeat_fresh
        ? `${result.status}. ${result.execution_owner || 'Execution Owner'} confirmed current.`
        : `${result.status}. Verify the Execution Owner heartbeat before market open.`;
      status.className = result.execution_owner_heartbeat_fresh
        ? 'mobile-inline-status success' : 'mobile-inline-status error';
    } catch (error) {
      status.textContent = `Publish blocked — ${error.message}`;
      status.className = 'mobile-inline-status error';
    } finally {
      button.textContent = "Publish Today's Plan";
      renderBuyBoardPage();
    }
  }

  async function toggleBuyTodayDraft() {
    if (!state.symbol || planningPending() || operatorPending()) return;
    const actionSymbol = state.symbol;
    const canonicalActive = canonicalBuyTodayActive();
    const hasDraft = hasCurrentBuyTodayDraft() && !canonicalActive;
    if ((state.plan?.canonical_stage || state.plan?.stage) === 'CLOSED'
        && operatorOperationEnabled('activate_buy_today')) {
      await toggleCanonicalBuyToday();
      return;
    }
    if (!canonicalActive && !hasDraft && !state.plan?.buylist_member) {
      if (!state.plan?.breakout_price) {
        requestBreakoutForList('buy_today');
        return;
      }
      const promoted = await moveToList('buylist');
      if (!promoted || state.symbol !== actionSymbol) return;
    }
    if (
      canonicalActive
      || (!hasDraft && operatorOperationEnabled('activate_buy_today'))
    ) {
      await toggleCanonicalBuyToday();
      return;
    }
    if (!hasCurrentBuyTodayDraft()) {
      await moveToList('buy_today');
      return;
    }
    const previousRow = state.buyTodayRows.find(row => row.symbol === actionSymbol);
    const expectedRevision = state.plan.version;
    state.planningPendingSymbols.add(actionSymbol);
    bumpPlanningEpoch(actionSymbol);
    state.planningBusy = planningPending();
    setOptimisticBuyToday(false, state.plan, actionSymbol);
    renderPlan(true);
    setPanel('Cancelling shared Buy Today draft…');
    try {
      await cancelBuyTodayDraft(actionSymbol, expectedRevision);
      state.optimisticBuyToday.delete(actionSymbol);
      state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== actionSymbol);
      if (state.symbol === actionSymbol) {
        setPanel('BUY TODAY DRAFT CANCELLED - stock remains in Buylist.', 'success');
      }
      void refreshPlanningLists().catch(() => {});
    } catch (error) {
      state.optimisticBuyToday.delete(actionSymbol);
      state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== actionSymbol);
      if (previousRow) state.buyTodayRows.unshift(previousRow);
      if (state.listMode === 'buy_today') applyListMode('buy_today');
      if (state.symbol === actionSymbol) {
        if (error.status === 409 && error.current) {
          state.plan = Object.keys(error.current).length ? error.current : null;
        }
        setPanel(`UNAVAILABLE - ${error.message}`, 'error');
      }
    } finally {
      bumpPlanningEpoch(actionSymbol);
      state.planningPendingSymbols.delete(actionSymbol);
      state.planningBusy = planningPending();
      refreshActivePlanningList();
      if (state.symbol === actionSymbol) renderPlan();
    }
  }

  async function moveToList(target) {
    if (!state.symbol || planningPending()) return false;
    const actionSymbol = state.symbol;
    const previousPlan = state.plan ? {...state.plan} : null;
    const previousBuyTodayRow = state.buyTodayRows.find(
      row => row.symbol === actionSymbol
    );
    const expectedRevision = previousPlan?.version || 0;
    const watchlistMember = previousPlan?.watchlist_member ?? (
      previousPlan?.stage === 'WATCHLIST' || previousPlan?.stage === 'BUYLIST'
    );
    const buylistMember = previousPlan?.buylist_member ?? (previousPlan?.stage === 'BUYLIST');
    const hadBuyTodayDraft = hasCurrentBuyTodayDraft();
    if (
      ['buylist', 'buy_today'].includes(target)
      && !buylistMember
      && !previousPlan?.breakout_price
    ) {
      requestBreakoutForList(target);
      return false;
    }
    let optimisticOperation = '';
    if (target === 'watchlist') {
      optimisticOperation = watchlistMember ? 'remove_watchlist' : 'add_watchlist';
    } else if (target === 'buylist') {
      optimisticOperation = buylistMember ? 'remove_buylist' : 'promote_buylist';
    } else {
      optimisticOperation = buylistMember ? '' : 'promote_buylist';
    }
    const optimistic = optimisticOperation
      ? optimisticPlan(optimisticOperation, null, previousPlan, actionSymbol)
      : {...previousPlan, _optimistic: true};
    if (target === 'buy_today') optimistic._optimisticOperation = 'buy_today_draft';
    state.planningPendingSymbols.add(actionSymbol);
    bumpPlanningEpoch(actionSymbol);
    state.optimisticPlans.set(actionSymbol, optimistic);
    state.planningBusy = planningPending();
    state.plan = optimistic;
    syncOptimisticPlanningRow(optimistic, actionSymbol);
    if (target === 'buy_today') {
      setOptimisticBuyToday(true, optimistic, actionSymbol);
    }
    if (target === 'buylist' && buylistMember && hadBuyTodayDraft) {
      setOptimisticBuyToday(false, optimistic, actionSymbol);
    }
    renderPlan(true);
    setPanel('Saving to canonical store…');
    let successMessage = 'SAVED TO CANONICAL STORE.';
    let committedPlan = previousPlan;
    try {
      if (target === 'watchlist') {
        const operation = watchlistMember ? 'remove_watchlist' : 'add_watchlist';
        const result = await sendPlanningCommand(
          actionSymbol, operation, null, expectedRevision
        );
        committedPlan = result.card;
        successMessage = watchlistMember
          ? 'REMOVED FROM WATCHLIST. BREAKOUT AND BUYLIST WERE KEPT.'
          : 'ADDED TO WATCHLIST.';
      } else if (target === 'buylist' && buylistMember) {
        if (hadBuyTodayDraft) {
          await cancelBuyTodayDraft(actionSymbol, expectedRevision);
        }
        const result = await sendPlanningCommand(
          actionSymbol, 'remove_buylist', null, expectedRevision
        );
        committedPlan = result.card;
        successMessage = watchlistMember
          ? 'REMOVED FROM BUYLIST - stock remains in Watchlist.'
          : 'REMOVED FROM BUYLIST - Watchlist was unchanged and breakout was kept.';
      } else {
        if (!buylistMember) {
          const result = await sendPlanningCommand(
            actionSymbol, 'promote_buylist', null, expectedRevision
          );
          committedPlan = result.card;
        }
        if (target === 'buy_today') {
          await saveBuyTodayDraft(actionSymbol, committedPlan.version);
          successMessage = 'SHARED BUY TODAY DRAFT - visible on other web devices, never activated or executable.';
        }
      }
      state.optimisticPlans.delete(actionSymbol);
      state.optimisticBuyToday.delete(actionSymbol);
      syncOptimisticPlanningRow(committedPlan, actionSymbol);
      if (target === 'buy_today') {
        state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== actionSymbol);
        state.buyTodayRows.unshift({
          symbol: actionSymbol,
          name: committedPlan.name || selectedSymbolName(actionSymbol),
          breakout_price: committedPlan.breakout_price,
          card_version: committedPlan.version,
        });
      } else if (target === 'buylist' && buylistMember && hadBuyTodayDraft) {
        state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== actionSymbol);
      }
      if (state.symbol === actionSymbol) {
        state.plan = committedPlan;
        setPanel(successMessage, 'success');
      }
      void refreshPlanningLists().catch(() => {});
      return true;
    } catch (error) {
      state.optimisticPlans.delete(actionSymbol);
      state.optimisticBuyToday.delete(actionSymbol);
      let restored = previousPlan;
      if (error.status === 409 && error.current) {
        restored = Object.keys(error.current).length ? error.current : null;
      }
      syncOptimisticPlanningRow(restored, actionSymbol);
      state.buyTodayRows = state.buyTodayRows.filter(row => row.symbol !== actionSymbol);
      if (previousBuyTodayRow) state.buyTodayRows.unshift(previousBuyTodayRow);
      if (state.symbol === actionSymbol) {
        state.plan = restored;
        setPanel(`UNAVAILABLE - ${error.message}`, 'error');
      }
      void refreshPlanningLists().catch(() => {});
      return false;
    } finally {
      bumpPlanningEpoch(actionSymbol);
      state.planningPendingSymbols.delete(actionSymbol);
      state.planningBusy = planningPending();
      refreshActivePlanningList();
      if (state.symbol === actionSymbol) renderPlan();
    }
  }

  function renderDrawingList() {
    const target = byId('drawing-list');
    target.replaceChildren();
    byId('drawing-count').textContent = String(state.drawings.length);
    state.drawings.forEach(drawing => {
      const row = document.createElement('div');
      row.className = 'drawing-row';
      const label = document.createElement('span');
      label.textContent = `1D + 1H · ${drawing.start_price.toFixed(2)} → ${drawing.end_price.toFixed(2)} · r${drawing.revision}`;
      const edit = document.createElement('button');
      edit.textContent = 'Nudge +1%';
      edit.addEventListener('click', () => updateDrawing(drawing));
      const remove = document.createElement('button');
      remove.className = 'delete';
      remove.textContent = 'Delete';
      remove.addEventListener('click', () => deleteDrawing(drawing));
      row.append(label, edit, remove);
      target.appendChild(row);
    });
    updateDrawingSelectionUi();
  }

  async function persistDrawingUpdate(drawing, next, successMessage = 'SAVED LOCALLY — drawing edit.') {
    if (state.drawingSaving) return;
    state.drawingSaving = true;
    state.drawings = state.drawings.map(item => item.id === drawing.id ? {...item, ...next} : item);
    renderDrawings();
    setPanel('SAVING drawing edit…');
    try {
      const result = await api(`/api/v1/drawings/${encodeURIComponent(drawing.id)}`, {
        method: 'PUT',
        body: JSON.stringify({
          expected_revision: drawing.revision,
          start_date: next.start_date, start_price: next.start_price,
          end_date: next.end_date, end_price: next.end_price,
          timeframe: next.timeframe,
        }),
      });
      state.drawings = state.drawings.map(item => item.id === drawing.id ? result.drawing : item);
      state.selectedDrawingId = drawing.id;
      renderDrawings();
      renderDrawingList();
      setPanel(successMessage, 'success');
    } catch (error) {
      setPanel(`${error.status === 409 ? 'CONFLICT' : 'UNAVAILABLE'} — ${error.message}`, 'error');
      await fetchPlanAndDrawings(state.symbol, state.selectionToken);
    } finally {
      state.drawingSaving = false;
    }
  }

  async function updateDrawing(drawing) {
    await persistDrawingUpdate(drawing, {
      ...drawing,
      start_price: drawing.start_price * 1.01,
      end_price: drawing.end_price * 1.01,
    });
  }

  async function deleteDrawing(drawing) {
    setPanel('Deleting drawing…');
    try {
      await api(`/api/v1/drawings/${encodeURIComponent(drawing.id)}`, {
        method: 'DELETE',
        body: JSON.stringify({expected_revision: drawing.revision}),
      });
      state.drawings = state.drawings.filter(item => item.id !== drawing.id);
      if (state.selectedDrawingId === drawing.id) state.selectedDrawingId = null;
      renderDrawings();
      renderDrawingList();
      setPanel('Drawing tombstone saved locally.', 'success');
    } catch (error) {
      setPanel(`${error.status === 409 ? 'CONFLICT' : 'UNAVAILABLE'} — ${error.message}`, 'error');
    }
  }

  function stepSymbol(direction) {
    if (!state.visibleRows.length) return;
    const current = state.visibleRows.findIndex(row => row.symbol === state.symbol);
    if (current < 0) {
      const continuation = listContinuationSymbol(direction);
      if (continuation) {
        void selectSymbol(continuation, direction < 0 ? 'previous-stock' : 'next-stock');
        return;
      }
    }
    const next = current < 0
      ? (direction < 0 ? state.visibleRows.length - 1 : 0)
      : (current + direction + state.visibleRows.length) % state.visibleRows.length;
    if (state.visibleRows[next]) {
      selectSymbol(state.visibleRows[next].symbol, direction < 0 ? 'previous-stock' : 'next-stock');
    }
  }

  function updateTimeframeButtons() {
    document.querySelectorAll('[data-timeframe]').forEach(button => button.classList.toggle('active', button.dataset.timeframe === state.timeframe));
  }

  function updatePaneTimeframeToggle(labelId, timeframe) {
    const button = byId(labelId);
    const nextTimeframe = timeframe === '1D' ? '1H' : '1D';
    button.textContent = timeframe;
    button.dataset.currentTimeframe = timeframe;
    button.setAttribute('aria-label', `Switch chart to ${nextTimeframe}`);
    button.title = `Switch chart to ${nextTimeframe}`;
  }

  async function switchTimeframe(timeframe) {
    timeframe = String(timeframe || '').toUpperCase();
    if (!['1D', '1H'].includes(timeframe)) return;
    const startedAt = performance.now();
    state.drawAnchor = null;
    state.selectedDrawingId = null;
    updateDrawingSelectionUi();
    Object.values(state.panes).forEach(pane => { if (pane) pane.drawingPointer = null; });
    state.timeframe = timeframe;
    state.split = false;
    byId('chart-grid').classList.remove('split');
    localStorage.setItem('quant-web-timeframe', timeframe);
    updateTimeframeButtons();
    updatePaneTimeframeToggle('primary-label', timeframe);
    if (!state.symbol) return;
    const chartCacheHit = state.bundleCache.has(`${state.symbol}:${timeframe}`);
    state.selectionToken += 1;
    const token = state.selectionToken;
    const chartReady = await loadCharts(state.symbol, token);
    await nextPaint();
    if (token === state.selectionToken) {
      recordClientTiming('timeframe-switch-chart-paint', startedAt, {
        symbol: state.symbol,
        timeframe,
        cache_hit: chartCacheHit,
        chart_ready: chartReady === true,
      });
    }
    schedulePrefetch(token);
  }

  function wireEvents() {
    ['stock-list', 'mobile-list-items'].forEach(id => {
      const list = byId(id);
      list.addEventListener('scroll', () => {
        if (list.clientHeight) {
          state.listScrollPositions.set(`${state.listMode}:${id}`, list.scrollTop);
        }
      }, {passive: true});
    });
    document.addEventListener('click', cancelDrawingModeForOtherControl, true);
    document.querySelectorAll('.list-tab').forEach(button => button.addEventListener('click', () => activateListMode(button.dataset.list)));
    const mobileListMenu = byId('mobile-list-menu');
    const mobileListPopover = byId('mobile-list-popover');
    const mobileMenuPopover = byId('mobile-menu-popover');
    const closeMobileListPopover = () => {
      mobileListPopover.hidden = true;
      mobileListMenu.setAttribute('aria-expanded', 'false');
    };
    const closeMobileMenuPopover = () => {
      mobileMenuPopover.hidden = true;
    };
    document.querySelectorAll('.mobile-workspace-tab').forEach(button => {
      button.addEventListener('click', () => setMobilePage(button.dataset.mobilePage));
    });
    byId('pulse-open-chart').addEventListener('click', () => setMobilePage('chart'));
    byId('summary-open-lists').addEventListener('click', () => {
      setMobilePage('chart');
      mobileListMenu.click();
    });
    byId('summary-open-settings').addEventListener('click', () => {
      closeMobileListPopover();
      mobileMenuPopover.hidden = false;
      syncDisplaySettingInputs();
    });
    byId('mobile-orb-settings-form').addEventListener('submit', saveOrbSettings);
    byId('orb-settings-defaults').addEventListener('click', () => {
      setOrbSettingsInputs(ORB_SETTINGS_DEFAULTS);
      const status = byId('orb-settings-status');
      status.textContent = 'Defaults loaded. Tap Save shared settings to apply them.';
      status.className = 'mobile-inline-status';
    });
    Object.values(ORB_INPUTS).forEach(id => byId(id).addEventListener('input', event => {
      event.target.setCustomValidity('');
    }));
    byId('publish-today-plan').addEventListener('click', publishTodayPlan);
    byId('buy-board-refresh').addEventListener('click', () => loadBuyBoard(false));
    byId('buy-board-action-close').addEventListener('click', closeBuyBoardActionSheet);
    byId('buy-board-action-sheet').addEventListener('pointerdown', event => {
      if (event.target === byId('buy-board-action-sheet')) closeBuyBoardActionSheet();
    });
    byId('buy-board-open-chart').addEventListener('click', () => {
      const symbol = state.buyBoardSelectedSymbol;
      closeBuyBoardActionSheet();
      if (!symbol) return;
      setMobilePage('chart');
      void selectSymbol(symbol);
    });
    byId('buy-board-action-input-submit').addEventListener('click', () => {
      const wrapper = byId('buy-board-action-input');
      const input = byId('buy-board-action-value');
      const value = Number(input.value);
      const action = wrapper.dataset.action;
      if (!Number.isFinite(value) || value <= 0 || (action === 'request_partial_sell' && !Number.isInteger(value))) {
        input.setCustomValidity(action === 'request_partial_sell' ? 'Enter a whole number of shares.' : 'Enter a positive price.');
        input.reportValidity();
        return;
      }
      input.setCustomValidity('');
      wrapper.hidden = true;
      void applyBuyBoardAction(action, action === 'request_partial_sell' ? {quantity: value} : {price: value});
    });
    byId('buy-board-action-value').addEventListener('input', event => event.target.setCustomValidity(''));
    byId('operator-confirm-cancel').addEventListener('click', () => closeOperatorConfirm(false));
    byId('desktop-orb-settings').addEventListener('click', () => {
      setMobilePage('summary');
      void loadOrbSettings();
    });
    byId('desktop-orb-settings-close').addEventListener('click', () => setMobilePage('chart'));
    byId('operator-confirm-submit').addEventListener('click', () => closeOperatorConfirm(true));
    byId('operator-confirm-ep').addEventListener('click', () => closeOperatorConfirm('ep'));
    byId('operator-confirm-dialog').addEventListener('pointerdown', event => {
      if (event.target === byId('operator-confirm-dialog')) closeOperatorConfirm(false);
    });
    mobileListMenu.addEventListener('click', () => {
      const opening = mobileListPopover.hidden;
      closeMobileMenuPopover();
      if (opening && state.mobilePage !== 'chart') setMobilePage('chart');
      mobileListPopover.hidden = !opening;
      mobileListMenu.setAttribute('aria-expanded', String(opening));
      if (opening) {
        applyListMode(state.listMode);
        if (state.listMode === 'monitor') void loadIntradayMonitor();
        renderMobileStockList();
        requestAnimationFrame(() => {
          const selected = mobileListPopover.querySelector('.mobile-list-row.active');
          if (selected) selected.scrollIntoView({block: 'nearest'});
        });
      }
    });
    document.querySelectorAll('[data-display-setting]').forEach(input => {
      input.addEventListener('change', () => {
        state.displayPreferences[input.dataset.displaySetting] = input.checked;
        saveDisplayPreferences();
        applyDisplayPreferences(true);
      });
    });
    byId('display-settings-reset').addEventListener('click', () => {
      state.displayPreferences = {...DISPLAY_PREFERENCE_DEFAULTS};
      saveDisplayPreferences();
      applyDisplayPreferences(true);
    });
    document.querySelectorAll('[data-mobile-list]').forEach(button => button.addEventListener('click', () => {
      activateListMode(button.dataset.mobileList);
      byId('mobile-list-items').scrollTop = 0;
    }));
    document.querySelectorAll('[data-monitor-filter]').forEach(button => button.addEventListener('click', () => {
      state.monitorFilter = button.dataset.monitorFilter;
      document.querySelectorAll('[data-monitor-filter]').forEach(filter => {
        const active = filter.dataset.monitorFilter === state.monitorFilter;
        filter.classList.toggle('active', active);
        filter.setAttribute('aria-pressed', String(active));
      });
      applyListMode('monitor');
    }));
    byId('mobile-monitor-search').addEventListener('input', event => {
      state.monitorQuery = event.target.value.trim().toLowerCase();
      applyListMode('monitor');
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) void loadIntradayMonitor();
    });
    document.querySelectorAll('[data-history-form]').forEach(form => form.addEventListener('submit', event => {
      event.preventDefault();
      const scope = form.dataset.historyForm;
      loadWatchlistHistory(
        byId(`${scope}-history-from`).value,
        byId(`${scope}-history-to`).value,
      );
    }));
    document.addEventListener('pointerdown', event => {
      if (!mobileListPopover.hidden && !event.target.closest('.mobile-list-control')) closeMobileListPopover();
      if (!mobileMenuPopover.hidden && !event.target.closest('#mobile-menu-popover, #summary-open-settings')) closeMobileMenuPopover();
      const breakoutPopup = byId('breakout-price-popup');
      if (!breakoutPopup.hidden && event.target === breakoutPopup) closeBreakoutPricePopup();
    });
    document.querySelectorAll('[data-timeframe]').forEach(button => button.addEventListener('click', () => switchTimeframe(button.dataset.timeframe)));
    document.querySelectorAll('[data-chart-timeframe-toggle]').forEach(button => button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      void switchTimeframe(button.dataset.currentTimeframe === '1D' ? '1H' : '1D');
    }));
    byId('market-alignment-toggle').addEventListener('click', event => {
      event.stopPropagation();
      setMarketAlignmentExpanded(!state.alignmentOpen);
    });
    byId('mobile-previous-symbol').addEventListener('click', () => stepSymbol(-1));
    byId('mobile-next-symbol').addEventListener('click', () => stepSymbol(1));
    byId('previous-symbol').addEventListener('click', () => stepSymbol(-1));
    byId('next-symbol').addEventListener('click', () => stepSymbol(1));
    byId('fit-chart').addEventListener('click', () => Object.values(state.panes).forEach(resetChartRange));
    byId('rail-collapse').addEventListener('click', () => byId('stock-rail').classList.toggle('collapsed'));
    byId('split-toggle').addEventListener('click', async () => {
      state.split = !state.split;
      byId('chart-grid').classList.toggle('split', state.split);
      if (state.split && state.symbol) {
        state.timeframe = '1D';
        updateTimeframeButtons();
        state.selectionToken += 1;
        await loadCharts(state.symbol, state.selectionToken);
      }
    });
    const toggleLineTool = () => {
      setBreakoutMode(false, false);
      setDrawingMode(!state.drawMode);
    };
    byId('draw-line').addEventListener('click', toggleLineTool);
    byId('mobile-draw-line').addEventListener('click', toggleLineTool);
    byId('mobile-delete-drawing').addEventListener('click', async () => {
      const drawing = selectedDrawing();
      if (!drawing) return;
      await deleteDrawing(drawing);
    });
    byId('mobile-cancel-drawing-selection').addEventListener('click', () => {
      state.selectedDrawingId = null;
      state.drawingDrag = null;
      renderDrawings();
      updateDrawingSelectionUi();
      setPanel('Drawing selection cleared.');
    });
    byId('place-breakout').addEventListener('click', toggleBreakoutPlacement);
    byId('add-watchlist').addEventListener('click', () => planningCommand('add_watchlist'));
    byId('promote-buylist').addEventListener('click', () => planningCommand('promote_buylist'));
    byId('move-watchlist').addEventListener('click', () => planningCommand('move_watchlist'));
    byId('remove-watchlist').addEventListener('click', () => planningCommand('remove_watchlist'));
    byId('quick-watchlist').addEventListener('click', () => moveToList('watchlist'));
    byId('quick-buylist').addEventListener('click', () => moveToList('buylist'));
    byId('quick-buy-today').addEventListener('click', toggleBuyTodayDraft);
    byId('quick-plan-details').addEventListener('click', toggleBreakoutPlacement);
    const saveManualBreakout = () => {
      const value = Number(byId('breakout-input').value);
      if (!Number.isFinite(value) || value <= 0) return setPanel('Enter a positive breakout price.', 'error');
      setBreakoutMode(false, false);
      commitBreakoutPrice(value, `Breakout set to ${value.toFixed(2)} — saved locally.`);
    };
    byId('save-breakout').addEventListener('click', saveManualBreakout);
    byId('breakout-input').addEventListener('keydown', event => {
      if (event.key === 'Enter') saveManualBreakout();
    });
    byId('breakout-price-form').addEventListener('submit', async event => {
      event.preventDefault();
      const input = byId('breakout-price-popup-input');
      const value = Number(input.value);
      if (!Number.isFinite(value) || value <= 0) {
        input.setCustomValidity('Enter a positive breakout price.');
        input.reportValidity();
        return;
      }
      input.setCustomValidity('');
      const actionSymbol = state.symbol;
      const followup = state.breakoutFollowup;
      closeBreakoutPricePopup();
      const saved = await commitBreakoutPrice(value, `Breakout set to ${value.toFixed(2)} — saved.`);
      if (!saved || state.symbol !== actionSymbol || followup?.symbol !== actionSymbol) return;
      if (followup.target === 'buy_today') await toggleBuyTodayDraft();
      else await moveToList('buylist');
    });
    byId('breakout-price-popup-input').addEventListener('input', event => event.target.setCustomValidity(''));
    byId('breakout-price-cancel').addEventListener('click', closeBreakoutPricePopup);
    byId('breakout-price-close').addEventListener('click', closeBreakoutPricePopup);
    byId('clear-breakout').addEventListener('click', () => {
      setBreakoutMode(false, false);
      state.breakoutDraftPrice = null;
      planningCommand('clear_breakout');
    });
    byId('buy-today-preview').addEventListener('click', toggleBuyTodayDraft);
    byId('close-planning').addEventListener('click', () => byId('planning-panel').classList.remove('open'));
    const logout = async () => {
      try { await api('/api/v1/auth/logout', {method: 'POST'}); } finally {
        if (navigator.serviceWorker?.controller) navigator.serviceWorker.controller.postMessage('CLEAR_CACHES');
        window.location.replace('/login');
      }
    };
    byId('logout-button').addEventListener('click', logout);
    byId('mobile-logout-button').addEventListener('click', logout);

    const mobileSymbolSearchDialog = byId('mobile-symbol-search-dialog');
    const mobileSymbolSearchButton = byId('mobile-symbol-search-button');
    const activeSymbolSearchTrigger = byId('active-symbol-search-trigger');
    const closeMobileSymbolSearch = () => {
      mobileSymbolSearchDialog.hidden = true;
      mobileSymbolSearchButton.setAttribute('aria-expanded', 'false');
      activeSymbolSearchTrigger.setAttribute('aria-expanded', 'false');
      byId('mobile-symbol-search').value = '';
      byId('mobile-symbol-search-results').replaceChildren();
      byId('mobile-symbol-search-results').hidden = true;
    };
    const openSymbolSearch = () => {
      if (!compactLayout.matches && !standaloneLayout.matches) {
        byId('symbol-search').focus({preventScroll: true});
        return;
      }
      closeMobileListPopover();
      closeMobileMenuPopover();
      mobileSymbolSearchDialog.hidden = false;
      mobileSymbolSearchButton.setAttribute('aria-expanded', 'true');
      activeSymbolSearchTrigger.setAttribute('aria-expanded', 'true');
      requestAnimationFrame(() => byId('mobile-symbol-search').focus({preventScroll: true}));
    };
    mobileSymbolSearchButton.addEventListener('click', openSymbolSearch);
    activeSymbolSearchTrigger.addEventListener('click', openSymbolSearch);
    byId('mobile-symbol-search-close').addEventListener('click', closeMobileSymbolSearch);
    mobileSymbolSearchDialog.addEventListener('pointerdown', event => {
      if (event.target === mobileSymbolSearchDialog) closeMobileSymbolSearch();
    });

    const openSearchedSymbol = (item, input, target, onSelected) => {
      target.hidden = true;
      input.value = '';
      if (!state.scanner.some(row => row.symbol === item.symbol) && !state.manualRows.some(row => row.symbol === item.symbol)) {
        state.manualRows.unshift({symbol: item.symbol, name: item.company, score: null, rank: '•'});
        applyListMode('scanner');
      }
      onSelected?.();
      selectSymbol(item.symbol);
    };

    const bindSymbolSearch = (inputId, resultsId, onSelected = null) => {
      const input = byId(inputId);
      const target = byId(resultsId);
      let timer = null;
      let requestToken = 0;
      input.addEventListener('input', event => {
        clearTimeout(timer);
        requestToken += 1;
        const token = requestToken;
        const query = event.target.value.trim();
        if (!query) {
          target.replaceChildren();
          target.hidden = true;
          return;
        }
        timer = setTimeout(async () => {
          try {
            const result = await api(`/api/v1/search?q=${encodeURIComponent(query)}&limit=20`);
            if (token !== requestToken) return;
            target.replaceChildren();
            (result.results || []).forEach(item => {
              const button = document.createElement('button');
              button.type = 'button';
              button.className = 'search-result';
              button.setAttribute('role', 'option');
              const symbol = document.createElement('strong');
              symbol.textContent = item.symbol;
              const detail = document.createElement('small');
              detail.textContent = `${item.company || item.symbol} · ${item.exchange || 'UNAVAILABLE'}`;
              button.append(symbol, detail);
              button.addEventListener('click', () => openSearchedSymbol(item, input, target, onSelected));
              target.appendChild(button);
            });
            target.hidden = !(result.results || []).length;
          } catch (error) {
            if (token !== requestToken) return;
            target.replaceChildren();
            const status = document.createElement('div');
            status.className = 'search-result-status';
            status.textContent = `Search unavailable: ${error.message}`;
            target.appendChild(status);
            target.hidden = false;
          }
        }, 250);
      });
      input.addEventListener('keydown', event => {
        if (event.key !== 'Enter') return;
        const first = target.querySelector('.search-result');
        if (!first) return;
        event.preventDefault();
        first.click();
      });
    };
    bindSymbolSearch('symbol-search', 'search-results');
    bindSymbolSearch('mobile-symbol-search', 'mobile-symbol-search-results', closeMobileSymbolSearch);

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !byId('buy-board-action-sheet').hidden) {
        event.preventDefault();
        closeBuyBoardActionSheet();
        return;
      }
      if (event.key === 'Escape' && !mobileSymbolSearchDialog.hidden) {
        event.preventDefault();
        closeMobileSymbolSearch();
        return;
      }
      if (event.key === 'Escape' && !byId('breakout-price-popup').hidden) {
        event.preventDefault();
        closeBreakoutPricePopup();
        return;
      }
      const target = event.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable) return;
      if (event.key.toLowerCase() === 'd') {
        event.preventDefault();
        setBreakoutMode(false, false);
        setDrawingMode(!state.drawMode);
        return;
      }
      if (event.key === 'Escape' && state.drawMode) {
        event.preventDefault();
        setDrawingMode(false);
        return;
      }
      if ((event.key === 'Delete' || event.key === 'Backspace') && state.selectedDrawingId) {
        const drawing = state.drawings.find(item => item.id === state.selectedDrawingId);
        if (drawing) {
          event.preventDefault();
          deleteDrawing(drawing);
        }
        return;
      }
      if (event.key === 'ArrowLeft') stepSymbol(-1);
      if (event.key === 'ArrowRight') stepSymbol(1);
    });
    window.addEventListener('offline', () => {
      byId('browser-dot').classList.remove('good');
      setGlobal('Browser offline — planning is read-only until reconnected.');
    });
    window.addEventListener('online', () => {
      byId('browser-dot').classList.add('good');
      setGlobal('Browser reconnected. Reloading current state.');
      refreshStatusStrip();
      if (state.symbol) fetchPlanAndDrawings(state.symbol, state.selectionToken);
    });
    window.addEventListener('pagehide', () => {
      if (state.liveUpdateReconnectTimer !== null) window.clearTimeout(state.liveUpdateReconnectTimer);
      state.liveUpdateReconnectTimer = null;
      if (state.liveUpdateSocket) state.liveUpdateSocket.close();
      state.liveUpdateSocket = null;
    });
    const layoutChanged = () => {
      renderStockList();
      updateDrawingSelectionUi();
      Object.values(state.panes).forEach(pane => {
        applyCompactRelativeLabels(pane);
        renderPaneDrawings(pane);
      });
    };
    if (compactLayout.addEventListener) compactLayout.addEventListener('change', layoutChanged);
    else compactLayout.addListener(layoutChanged);
  }

  async function initialize() {
    prepareServiceWorker().catch(() => {});
    lockPageDragging();
    initializeHistoryRange();
    state.panes.primary = createPane('chart-primary', 'rs-primary', 'primary-overlay', state.timeframe);
    state.panes.secondary = createPane('chart-secondary', 'rs-secondary', 'secondary-overlay', '1H');
    applyDisplayPreferences();
    updateTimeframeButtons();
    wireEvents();
    if (!compactLayout.matches) applyListMode('watchlist');
    setMobilePage('chart');
    try {
      state.session = await api('/api/v1/session');
      byId('quick-safety').textContent = state.session.mode === 'CONNECTED' ? 'PC SYNC' : 'LOCAL ONLY';
      byId('planning-eyebrow').textContent = state.session.mode === 'CONNECTED' ? 'PC PLANNING' : 'LOCAL PLANNING';
      connectLiveUpdates();
      startStatusStripRefresh();
      startIntradayMonitorPolling();
      await Promise.all([loadScanner(), refreshPlanningLists(), loadOrbSettings()]);
      const remembered = localStorage.getItem('quant-web-symbol');
      const initial = state.scanner.find(row => row.symbol === remembered)?.symbol || state.scanner[0]?.symbol;
      if (initial) await selectSymbol(initial);
      setGlobal(state.session.mode === 'CONNECTED'
        ? 'Workspace ready. PC market and canonical planning data are connected.'
        : 'Workspace ready. Sandbox changes persist locally.');
    } catch (error) {
      setGlobal(`Workspace unavailable: ${error.message}`);
      byId('service-dot').classList.remove('good');
    }
  }

  initialize();
  return true;
  }

  bootApp();
})();
