import test from 'node:test';
import assert from 'node:assert/strict';

import { matchesClientFilter, collectClientFilterOptions } from '../static/client_filters.js';

const clients = [
  {
    id: '1',
    name: 'Northwind',
    priority: 'must',
    account_manager: 'Ava Chen',
    product: 'Property',
    signals: { status: 'Active', health: 'Green' },
    summary: 'Large account',
    talking_points: ['Renewal'],
    avoid_points: ['None'],
    pocs: [{ name: 'Jill', title: 'VP', note: 'happy' }],
    tags: ['Property'],
  },
  {
    id: '2',
    name: 'Harborline',
    priority: 'watch',
    account_manager: 'Mateo Ruiz',
    product: 'Benefits',
    signals: { status: 'At Risk', health: 'Red' },
    summary: 'Escalation',
    talking_points: ['AI terms'],
    avoid_points: ['Do not mention fee'],
    pocs: [{ name: 'Noah', title: 'CIO', note: 'concerned' }],
    tags: ['Benefits'],
  },
];

test('collectClientFilterOptions derives values from the client dataset', () => {
  const options = collectClientFilterOptions(clients);

  assert.deepEqual(options.accountManagers, ['Ava Chen', 'Mateo Ruiz']);
  assert.deepEqual(options.ams, ['Benefits', 'Property']);
  assert.deepEqual(options.statuses, ['Active', 'At Risk']);
  assert.deepEqual(options.healths, ['Green', 'Red']);
});

test('matchesClientFilter combines search, priority, and dropdown filters', () => {
  assert.equal(
    matchesClientFilter(clients[0], 'north', ['must'], { accountManager: 'Ava Chen', ams: 'all', status: 'all', health: 'all' }),
    true,
  );
  assert.equal(
    matchesClientFilter(clients[0], '', ['must'], { accountManager: 'Ava Chen', ams: 'Property', status: 'Active', health: 'Green' }),
    true,
  );
  assert.equal(
    matchesClientFilter(clients[0], '', ['must'], { accountManager: 'Ava Chen', ams: 'Benefits', status: 'all', health: 'all' }),
    false,
  );
  assert.equal(
    matchesClientFilter(clients[1], 'concerned', ['watch'], { accountManager: 'Mateo Ruiz', ams: 'Benefits', status: 'At Risk', health: 'Red' }),
    true,
  );
});
