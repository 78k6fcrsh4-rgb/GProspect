/* ============================================================
   GrantScout AI — app.js
   Minimal vanilla JS for portal interactions
   ============================================================ */

/* ── Keyboard shortcuts ──────────────────────────────────── */
document.addEventListener('keydown', function (e) {
  // Escape closes any open panel
  if (e.key === 'Escape') {
    closeDetailPanel();
    closeAlertPanel();
    closeRunAgentModal();
  }
});

/* ── Detail panel helpers (also defined in dashboard.html
      but exposed globally here for safety) ─────────────── */
function closeDetailPanel() {
  var backdrop = document.getElementById('detailBackdrop');
  var panel    = document.getElementById('detailPanel');
  if (backdrop) backdrop.classList.remove('open');
  if (panel)    panel.classList.remove('open');
}

/* ── Alert panel helpers ─────────────────────────────────── */
function openAlertPanel() {
  var backdrop = document.getElementById('alertBackdrop');
  var panel    = document.getElementById('alertPanel');
  if (backdrop) backdrop.classList.add('open');
  if (panel)    panel.classList.add('open');
}

function closeAlertPanel() {
  var backdrop = document.getElementById('alertBackdrop');
  var panel    = document.getElementById('alertPanel');
  if (backdrop) backdrop.classList.remove('open');
  if (panel)    panel.classList.remove('open');
}

/* ── Run agent modal helpers ─────────────────────────────── */
function closeRunAgentModal() {
  var modal = document.getElementById('runAgentModal');
  if (modal) modal.classList.remove('open');
}

/* ── Role switcher ───────────────────────────────────────── */
function switchRole(role) {
  fetch('/api/switch-role', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ role: role })
  })
  .then(function () { window.location.reload(); })
  .catch(function (err) { console.error('Role switch failed:', err); });
}

/* ── Simulate alert ──────────────────────────────────────── */
function simulateAlert() {
  fetch('/api/simulate-alert', { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      showFlash(data.message || 'Alert simulated!', 'success');
    })
    .catch(function () { showFlash('Could not simulate alert.', 'error'); });
}

/* ── Flash message helper ────────────────────────────────── */
function showFlash(message, type) {
  var container = document.getElementById('flashContainer');
  if (!container) return;

  var el = document.createElement('div');
  el.className = 'flash-msg ' + (type || 'info');

  var icon = type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ';
  el.textContent = icon + '  ' + message;
  el.onclick = function () { el.remove(); };

  container.appendChild(el);

  setTimeout(function () {
    el.style.opacity    = '0';
    el.style.transition = 'opacity 0.3s ease';
    setTimeout(function () { el.remove(); }, 300);
  }, 4000);
}

// Expose globally
window.showFlash     = showFlash;
window.openAlertPanel  = openAlertPanel;
window.closeAlertPanel = closeAlertPanel;
window.closeDetailPanel = closeDetailPanel;

/* ── Auto-dismiss flash messages rendered by Flask ────────── */
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.flash-msg').forEach(function (el) {
    setTimeout(function () {
      el.style.opacity    = '0';
      el.style.transition = 'opacity 0.3s ease';
      setTimeout(function () { el.remove(); }, 300);
    }, 4000);
  });

  /* ── Prevent double-submit on any form ──────────────── */
  document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () {
      var btn = form.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.classList.add('btn-loading');
      }
    });
  });

  /* ── Confirm before delete buttons ──────────────────── */
  document.querySelectorAll('[data-confirm]').forEach(function (el) {
    el.addEventListener('click', function (e) {
      if (!confirm(el.getAttribute('data-confirm'))) {
        e.preventDefault();
        e.stopPropagation();
      }
    });
  });

  /* ── Active nav link highlighting (fallback) ─────────── */
  var path = window.location.pathname;
  document.querySelectorAll('.nav-item').forEach(function (link) {
    if (link.getAttribute('href') && path.startsWith(link.getAttribute('href'))) {
      link.classList.add('active');
    }
  });
});
