/* offline_queue.js - queue-and-retry wrapper for scan-triggered fetch() calls
 * that fail due to a network blip (this app runs on a desk-side Raspberry Pi
 * over Wi-Fi - see DEPLOYMENT.md - so a brief drop is expected, not
 * exceptional). Loaded globally in base.html, before ui.js.
 *
 * Contract: queuedFetch(url, options, meta) behaves exactly like fetch() when
 * the network is up (resolves to the real Response, HTTP error statuses like
 * 409/400 included - those are legitimate app-level responses, not queued).
 * It only queues on an actual network failure (fetch() throwing, or
 * navigator.onLine === false) - in that case it throws an OfflineQueuedError
 * instead of resolving, so callers can tell "the server said no" apart from
 * "this never reached the server yet."
 *
 * Only wire in an endpoint here if a retried duplicate POST is safe - see the
 * call sites in ui.js/checkout for the specific reasoning per endpoint
 * (add-to-cart is naturally deduped by a DB UNIQUE constraint; session-start
 * and checkout-finalize are NOT queued, see their call sites).
 *
 * Two ways a queued item resolves:
 * - Same tab, still open: the in-memory callback registered at enqueue time
 *   fires with the real response once the retry succeeds, so the UI updates
 *   normally (e.g. the cart row actually gets added).
 * - Tab was reloaded/reopened: only the persisted (url, options) survive -
 *   no callback to call. Those are drained silently in the background; the
 *   pending-count banner is the only feedback until the user next reloads
 *   whatever page reads that server-side state.
 */
(function () {
  var STORAGE_KEY = 'toolroom_offline_queue';
  var MAX_RETRIES = 5;
  var BASE_BACKOFF_MS = 2000;
  var pendingCallbacks = {}; // queue item id -> function(Response) - in-memory only, see module doc

  function OfflineQueuedError(message) {
    this.name = 'OfflineQueuedError';
    this.message = message;
    this.offlineQueued = true;
  }
  OfflineQueuedError.prototype = Object.create(Error.prototype);

  function loadQueue() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (err) {
      return [];
    }
  }

  function saveQueue(items) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    } catch (err) {
      // Storage full/unavailable (private window, quota) - the queue just
      // won't survive a reload; same-tab retry via pendingCallbacks still works.
    }
  }

  function queueLength() {
    return loadQueue().length;
  }

  function notifyQueueChanged() {
    document.dispatchEvent(new CustomEvent('offline-queue-changed', { detail: { length: queueLength() } }));
  }

  function isLikelyNetworkError(err) {
    // fetch() rejects with a generic TypeError ("Failed to fetch" / "NetworkError...")
    // for DNS/connection failures - there's no more specific error type to check.
    return err instanceof TypeError || navigator.onLine === false;
  }

  /**
   * @param {string} url
   * @param {object} options - fetch() init object
   * @param {object} [meta]
   * @param {string} [meta.label] - short human label used in the "will retry" toast
   * @returns {Promise<Response>}
   */
  function queuedFetch(url, options, meta) {
    meta = meta || {};
    if (navigator.onLine === false) {
      return Promise.reject(enqueue(url, options, meta));
    }
    return fetch(url, options).catch(function (err) {
      if (!isLikelyNetworkError(err)) throw err;
      throw enqueue(url, options, meta);
    });
  }

  function enqueue(url, options, meta) {
    var id = 'q' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    var item = {
      id: id,
      url: url,
      options: options,
      label: meta.label || 'a pending scan',
      queuedAt: Date.now(),
      attempts: 0,
    };
    var items = loadQueue();
    items.push(item);
    saveQueue(items);
    if (typeof meta.onSuccess === 'function') {
      pendingCallbacks[id] = meta.onSuccess;
    }
    notifyQueueChanged();
    if (typeof window.showToast === 'function') {
      window.showToast('Saved offline - ' + item.label + ' will sync when back online.', 'warning');
    }
    scheduleDrain(BASE_BACKOFF_MS);
    return new OfflineQueuedError('Queued for retry: ' + item.label);
  }

  var drainTimer = null;
  function scheduleDrain(delayMs) {
    if (drainTimer) return;
    drainTimer = setTimeout(function () {
      drainTimer = null;
      drainQueue();
    }, delayMs);
  }

  async function drainQueue() {
    if (navigator.onLine === false) return;
    var items = loadQueue();
    if (items.length === 0) return;

    var remaining = [];
    var syncedCount = 0;
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      try {
        var res = await fetch(item.url, item.options);
        if (res.ok || (res.status >= 400 && res.status < 500)) {
          // Either it worked, or the server has a definitive (non-retryable)
          // answer for it now (e.g. the cart/session it targeted no longer
          // exists) - either way, stop retrying this item.
          var cb = pendingCallbacks[item.id];
          if (cb) {
            try { cb(res); } catch (cbErr) { /* UI callback errors shouldn't break draining the rest */ }
            delete pendingCallbacks[item.id];
          }
          syncedCount++;
          continue;
        }
        throw new Error('Server error ' + res.status);
      } catch (err) {
        item.attempts++;
        if (item.attempts >= MAX_RETRIES) {
          // Give up on this one - drop it rather than retry forever, and
          // say so, since it's otherwise a silent data-loss point.
          delete pendingCallbacks[item.id];
          if (typeof window.showToast === 'function') {
            window.showToast('Could not sync ' + item.label + ' after several tries - please redo it.', 'error');
          }
          continue;
        }
        remaining.push(item);
      }
    }
    saveQueue(remaining);
    notifyQueueChanged();
    if (syncedCount > 0 && typeof window.showToast === 'function') {
      window.showToast('Synced ' + syncedCount + ' pending scan' + (syncedCount === 1 ? '' : 's') + '.', 'success');
    }
    if (remaining.length > 0) {
      // Exponential-ish backoff shared across the whole queue, capped.
      var nextDelay = Math.min(BASE_BACKOFF_MS * Math.pow(2, Math.min.apply(null, remaining.map(function (r) { return r.attempts; }))), 60000);
      scheduleDrain(nextDelay);
    }
  }

  window.addEventListener('online', function () { drainQueue(); });
  // Periodic safety net in case the 'online' event doesn't fire reliably
  // (some browsers/OS network-stack combinations are inconsistent about it).
  setInterval(function () { if (queueLength() > 0) drainQueue(); }, 20000);
  document.addEventListener('DOMContentLoaded', function () { if (queueLength() > 0) drainQueue(); });

  window.OfflineQueue = {
    queuedFetch: queuedFetch,
    isOfflineQueuedError: function (err) { return !!(err && err.offlineQueued); },
    getQueueLength: queueLength,
    drainNow: drainQueue,
  };
})();
