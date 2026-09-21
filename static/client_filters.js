export function normalizeFilterValue(value) {
  return String(value == null ? '' : value).trim();
}

export function collectClientFilterOptions(clients = []) {
  const accountManagers = [];
  const ams = [];
  const statuses = [];
  const healths = [];

  for (const client of clients) {
    const am = normalizeFilterValue(client.account_manager);
    if (am) accountManagers.push(am);

    const product = normalizeFilterValue(client.product);
    if (product) ams.push(product);

    const status = normalizeFilterValue((client.signals && (client.signals.status || client.status)) || client.status);
    if (status) statuses.push(status);

    const health = normalizeFilterValue((client.signals && (client.signals.health || client.health)) || client.health);
    if (health) healths.push(health);
  }

  return {
    accountManagers: [...new Set(accountManagers)].sort((a, b) => a.localeCompare(b)),
    ams: [...new Set(ams)].sort((a, b) => a.localeCompare(b)),
    statuses: [...new Set(statuses)].sort((a, b) => a.localeCompare(b)),
    healths: [...new Set(healths)].sort((a, b) => a.localeCompare(b)),
  };
}

export function matchesClientFilter(client, rawQuery = '', priorities = [], filters = {}) {
  const q = normalizeFilterValue(rawQuery).toLowerCase();
  const pri = Array.isArray(priorities) ? priorities : [];

  if (pri.length && !pri.includes(client.priority)) return false;

  const accountManager = normalizeFilterValue(filters.accountManager || 'all');
  if (accountManager && accountManager !== 'all'
    && normalizeFilterValue(client.account_manager).toLowerCase() !== accountManager.toLowerCase()) {
    return false;
  }

  const ams = normalizeFilterValue(filters.ams || 'all');
  if (ams && ams !== 'all'
    && normalizeFilterValue(client.product).toLowerCase() !== ams.toLowerCase()) {
    return false;
  }

  const status = normalizeFilterValue(filters.status || 'all');
  const clientStatus = normalizeFilterValue((client.signals && (client.signals.status || client.status)) || client.status);
  if (status && status !== 'all' && clientStatus.toLowerCase() !== status.toLowerCase()) {
    return false;
  }

  const health = normalizeFilterValue(filters.health || 'all');
  const clientHealth = normalizeFilterValue((client.signals && (client.signals.health || client.health)) || client.health);
  if (health && health !== 'all' && clientHealth.toLowerCase() !== health.toLowerCase()) {
    return false;
  }

  if (!q) return true;

  const hay = [
    client.name,
    client.owner,
    client.account_manager,
    client.location,
    client.product,
    client.summary,
    (client.talking_points || []).join(' '),
    (client.avoid_points || []).join(' '),
    (client.pocs || []).map((p) => `${p.name} ${p.title} ${p.note || ''}`).join(' '),
    (client.tags || []).join(' '),
  ].join(' ').toLowerCase();

  return hay.includes(q);
}
