'use strict';

// All user text goes onto the page through text nodes / textContent (see el()), never innerHTML.
// Money is integer minor units end to end; the helpers below do string and integer maths only.

const $app = document.getElementById('app');
const SVG_NS = 'http://www.w3.org/2000/svg';
let CURRENCIES = {}; // code -> minor-unit exponent

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked') e[k] = v;
    else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...kids);
  return e;
}

function svg(tag, attrs = {}, text) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

// ---- money -------------------------------------------------------------------

const exponentOf = (code) => CURRENCIES[code] ?? 2;

// "12.5" -> 1250 for exponent 2. Returns null for anything that isn't a plain decimal with
// at most `exp` places. No parseFloat: "0.1 + 0.2" style errors can't creep in.
function parseMinor(text, exp) {
  const m = /^(\d{1,12})(?:\.(\d+))?$/.exec(text.trim());
  if (!m) return null;
  const frac = m[2] ?? '';
  if (frac.length > exp) return null;
  return Number(m[1]) * 10 ** exp + (frac ? Number(frac.padEnd(exp, '0')) : 0);
}

function formatMinor(n, exp) {
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (exp === 0) return sign + abs;
  const unit = 10 ** exp;
  return `${sign}${Math.floor(abs / unit)}.${String(abs % unit).padStart(exp, '0')}`;
}

const money = (n, code) => `${formatMinor(n, exponentOf(code))} ${code}`;

function newKey() {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// ---- api ---------------------------------------------------------------------

async function api(method, path, body, headers = {}) {
  try {
    const res = await fetch(path, {
      method,
      headers: body === undefined ? headers : { 'content-type': 'application/json', ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = res.status === 204 ? null : await res.json().catch(() => null);
    return { ok: res.ok, status: res.status, data, replay: res.headers.get('idempotent-replay') === 'true' };
  } catch {
    return { ok: false, status: 0, data: { detail: 'Network error. Retrying is safe.' } };
  }
}

function errText(r) {
  const d = r.data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return 'Please check the form: ' + d.map((x) => x.msg).join('; ');
  return 'Something went wrong';
}

// ---- auth --------------------------------------------------------------------

function viewAuth() {
  let mode = 'login';
  const err = el('div', { class: 'err' });
  const email = el('input', { type: 'text', autocomplete: 'email', required: true });
  const name = el('input', { type: 'text', maxlength: 60, autocomplete: 'name' });
  const password = el('input', { type: 'password', autocomplete: 'current-password', required: true });
  const nameRow = el('div', {}, el('label', {}, 'Your name'), name);
  const submit = el('button', { class: 'primary', type: 'submit' }, 'Log in');
  const tabLogin = el('button', { type: 'button', class: 'on' }, 'Log in');
  const tabReg = el('button', { type: 'button' }, 'Register');
  const setMode = (m) => {
    mode = m;
    tabLogin.classList.toggle('on', m === 'login');
    tabReg.classList.toggle('on', m === 'register');
    nameRow.hidden = m === 'login';
    submit.textContent = m === 'login' ? 'Log in' : 'Create account';
    password.autocomplete = m === 'login' ? 'current-password' : 'new-password';
    err.textContent = '';
  };
  tabLogin.onclick = () => setMode('login');
  tabReg.onclick = () => setMode('register');
  nameRow.hidden = true;
  const form = el(
    'form',
    {
      onsubmit: async (e) => {
        e.preventDefault();
        const body = mode === 'login'
          ? { email: email.value, password: password.value }
          : { email: email.value, name: name.value, password: password.value };
        const r = await api('POST', mode === 'login' ? '/api/login' : '/api/register', body);
        if (!r.ok) {
          err.textContent = r.status === 429 ? 'Too many attempts. Wait a moment.' : errText(r);
          return;
        }
        route();
      },
    },
    el('div', { class: 'tabs' }, tabLogin, tabReg),
    nameRow,
    el('label', {}, 'Email'), email,
    el('label', {}, 'Password (10+ characters)'), password,
    submit, err,
  );
  $app.replaceChildren(
    el('div', { class: 'panel' }, el('h1', {}, '💸 Splitit'), el('p', { class: 'hook' }, "Friends don't let friends do math at dinner."), form),
  );
}

async function logout() {
  await api('POST', '/api/logout');
  location.hash = '#/';
  route();
}

// ---- home --------------------------------------------------------------------

function topBar(me, ...extra) {
  return el('div', { class: 'top' }, el('strong', {}, '💸 Splitit'), ...extra, el('span', { class: 'spacer' }),
    el('span', { class: 'muted' }, me.name), el('button', { onclick: logout }, 'Log out'));
}

async function viewHome(me) {
  const gr = await api('GET', '/api/groups');
  const err = el('div', { class: 'err' });
  const gname = el('input', { type: 'text', maxlength: 80, required: true, placeholder: 'Goa trip' });
  const gcur = el('select', {}, ...Object.keys(CURRENCIES).map((c) => el('option', { value: c }, c)));
  gcur.value = CURRENCIES.INR !== undefined ? 'INR' : 'USD';
  const code = el('input', { type: 'text', maxlength: 16, required: true, placeholder: 'ABCD2345' });
  const list = el('ul', { class: 'list' });
  for (const g of gr.data ?? []) {
    list.append(el('li', {}, el('a', { href: `#/g/${g.id}` }, g.name), el('span', { class: 'muted' }, g.currency)));
  }
  if (!list.children.length) list.append(el('li', { class: 'muted' }, 'No groups yet. Create one or join with a code.'));

  const create = el('form', {
    onsubmit: async (e) => {
      e.preventDefault();
      const r = await api('POST', '/api/groups', { name: gname.value, currency: gcur.value });
      if (!r.ok) return (err.textContent = errText(r));
      location.hash = `#/g/${r.data.id}`;
    },
  }, el('h2', {}, 'New group'), el('div', { class: 'row' }, el('div', { class: 'grow' }, gname), gcur, el('button', { class: 'primary' }, 'Create')));

  const join = el('form', {
    onsubmit: async (e) => {
      e.preventDefault();
      const r = await api('POST', '/api/groups/join', { code: code.value });
      if (!r.ok) return (err.textContent = r.status === 404 ? 'No group has that code.' : errText(r));
      location.hash = `#/g/${r.data.id}`;
    },
  }, el('h2', {}, 'Join with a code'), el('div', { class: 'row' }, el('div', { class: 'grow' }, code), el('button', { class: 'primary' }, 'Join')));

  $app.replaceChildren(topBar(me), el('div', { class: 'wrap' },
    el('div', { class: 'card' }, el('h2', {}, 'Your groups'), list),
    el('div', { class: 'cols' }, el('div', { class: 'card' }, create), el('div', { class: 'card' }, join)),
    err));
}

// ---- group dashboard -----------------------------------------------------------

function balanceChart(balances, currency) {
  // Layout: names | bars around a zero axis | amounts in their own right-aligned column,
  // so a long amount can never run into (or off the edge past) a bar.
  const rowH = 32;
  const nameW = 78;
  const valueW = 112;
  const W = 460;
  const H = balances.length * rowH + 12;
  const barArea = W - nameW - valueW;
  const mid = nameW + barArea / 2;
  const half = barArea / 2 - 4;
  const max = Math.max(1, ...balances.map((b) => Math.abs(b.net_minor)));
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Balance per person' });
  s.append(svg('line', { class: 'axis', x1: mid, x2: mid, y1: 2, y2: H - 2 }));
  balances.forEach((b, i) => {
    const y = 6 + i * rowH;
    const w = Math.round((Math.abs(b.net_minor) / max) * half);
    s.append(svg('text', { x: 2, y: y + 17 }, b.name.length > 10 ? b.name.slice(0, 9) + '…' : b.name));
    if (b.net_minor !== 0) {
      s.append(svg('rect', { class: b.net_minor > 0 ? 'pos' : 'neg', x: b.net_minor > 0 ? mid : mid - w, y, width: Math.max(w, 2), height: 22, rx: 3 }));
    }
    s.append(svg('text', { x: W - 2, y: y + 17, 'text-anchor': 'end' }, money(b.net_minor, currency)));
  });
  return s;
}

function activityText(a, names) {
  const d = a.detail;
  const who = (id) => names.get(id) ?? 'someone';
  switch (a.action) {
    case 'group_created': return 'created the group';
    case 'member_joined': return 'joined the group';
    case 'expense_added': return `added “${d.description}”: ${money(d.amount_minor, d.currency)} paid by ${who(d.paid_by)}, split ${d.split_type}`;
    case 'expense_deleted': return `deleted “${d.description}”`;
    case 'settlement_added': return `recorded a payment from ${who(d.from_user)} to ${who(d.to_user)}`;
    default: return a.action;
  }
}

async function viewGroup(me, gid) {
  const S = { me, gid, group: null, bal: null, expenses: [], activity: [], formKey: null };
  const settleKeys = new Map(); // keeps one idempotency key per payment until it succeeds
  const hold = el('div');
  $app.replaceChildren(hold);

  const balCard = el('div', { class: 'card' });
  const transfersCard = el('div', { class: 'card' });
  const expensesCard = el('div', { class: 'card' });
  const activityCard = el('div', { class: 'card activity' });
  const formCard = el('div', { class: 'card' });
  let formMembers = '';

  async function load() {
    const [g, b, e, a] = await Promise.all([
      api('GET', `/api/groups/${gid}`), api('GET', `/api/groups/${gid}/balances`),
      api('GET', `/api/groups/${gid}/expenses`), api('GET', `/api/groups/${gid}/activity`),
    ]);
    if (!g.ok) {
      $app.replaceChildren(topBar(me), el('div', { class: 'wrap' }, el('div', { class: 'card' }, el('h2', {}, 'Group not found'), el('a', { href: '#/' }, 'Back to your groups'))));
      return false;
    }
    S.group = g.data; S.bal = b.data; S.expenses = e.data; S.activity = a.data;
    return true;
  }

  function paint() {
    const { group, bal } = S;
    const names = new Map(group.members.map((m) => [m.id, m.name]));
    const cur = group.currency;

    balCard.replaceChildren(el('h2', {}, 'Balances'), el('div', { class: 'muted' }, `Positive = is owed, negative = owes. All in ${cur}.`), balanceChart(bal.balances, cur));

    const ul = el('ul', { class: 'list' });
    for (const t of bal.transfers) {
      const mine = t.from_user === me.id || t.to_user === me.id;
      const label = t.from_user === me.id ? 'I paid this' : 'Mark received';
      const btn = el('button', { class: 'primary', disabled: !mine, title: mine ? '' : 'Only the two people involved can record this' }, label);
      btn.onclick = async () => {
        const body = { from_user: t.from_user, to_user: t.to_user, amount_minor: t.amount_minor };
        const sig = JSON.stringify(body);
        if (!settleKeys.has(sig)) settleKeys.set(sig, newKey());
        btn.disabled = true;
        const r = await api('POST', `/api/groups/${gid}/settlements`, body, { 'Idempotency-Key': settleKeys.get(sig) });
        if (r.ok) { settleKeys.delete(sig); toast('Payment recorded'); await refresh(); } else { btn.disabled = false; toast(errText(r)); }
      };
      ul.append(el('li', {}, el('span', {}, `${t.from_name} pays ${t.to_name} `, el('span', { class: 'amt' }, money(t.amount_minor, cur))), btn));
    }
    if (!bal.transfers.length) ul.append(el('li', { class: 'muted' }, 'All settled up 🎉'));
    transfersCard.replaceChildren(el('h2', {}, 'Settle up'), el('div', { class: 'muted' }, 'The fewest payments that clear every balance.'), ul);

    const ex = el('ul', { class: 'list' });
    for (const e of S.expenses) {
      const converted = e.currency !== cur ? ` (${money(e.base_amount_minor, cur)})` : '';
      const del = el('button', { class: 'link danger' }, 'Delete');
      let armed = false;
      del.onclick = async () => {
        if (!armed) { armed = true; del.textContent = 'Sure?'; setTimeout(() => { armed = false; del.textContent = 'Delete'; }, 3000); return; }
        const r = await api('DELETE', `/api/groups/${gid}/expenses/${e.id}`);
        if (r.ok) { toast('Expense deleted'); await refresh(); } else toast(errText(r));
      };
      ex.append(el('li', {},
        el('div', {}, el('strong', {}, e.description), el('div', { class: 'muted' }, `${e.expense_date} · paid by ${e.paid_by_name} · ${e.split_type}`)),
        el('div', {}, el('span', { class: 'amt' }, money(e.amount_minor, e.currency) + converted), ' ', del)));
    }
    if (!S.expenses.length) ex.append(el('li', { class: 'muted' }, 'No expenses yet.'));
    expensesCard.replaceChildren(el('h2', {}, 'Expenses'), ex);

    const act = el('ul', { class: 'list' });
    for (const a of S.activity) {
      act.append(el('li', {}, el('strong', {}, a.actor), ' ' + activityText(a, names), el('div', { class: 'muted' }, new Date(a.created_at * 1000).toLocaleString())));
    }
    activityCard.replaceChildren(el('h2', {}, 'Activity log'), el('div', { class: 'muted' }, 'Append-only: entries can\'t be edited or removed.'), act);

    const sig = group.members.map((m) => m.id).join(',');
    if (sig !== formMembers) { formMembers = sig; formCard.replaceChildren(expenseForm(S, refresh)); }
  }

  async function refresh() { if (await load()) paint(); }

  if (!(await load())) return;
  const g = S.group;
  const copy = el('button', { onclick: async () => { try { await navigator.clipboard.writeText(g.invite_code); toast('Invite code copied'); } catch { toast(g.invite_code); } } }, 'Copy');
  $app.replaceChildren(
    topBar(me, el('a', { href: '#/' }, '← Groups'), el('strong', {}, g.name), el('span', { class: 'muted' }, g.currency)),
    el('div', { class: 'wrap' },
      el('div', { class: 'card row' }, el('span', { class: 'muted' }, 'Invite code'), el('span', { class: 'code' }, g.invite_code), copy,
        el('span', { class: 'grow' }), el('a', { href: `/api/groups/${gid}/export.csv` }, 'Export CSV')),
      el('div', { class: 'cols' }, balCard, transfersCard),
      el('div', { class: 'cols' }, formCard, expensesCard),
      activityCard));
  paint();
}

function expenseForm(S, onDone) {
  const { group, me } = S;
  const err = el('div', { class: 'err' });
  const desc = el('input', { type: 'text', maxlength: 100, required: true, placeholder: 'Dinner' });
  const amount = el('input', { type: 'text', inputmode: 'decimal', required: true, placeholder: '0.00' });
  const cur = el('select', {}, ...Object.keys(CURRENCIES).map((c) => el('option', { value: c }, c)));
  cur.value = group.currency;
  const paidBy = el('select', {}, ...group.members.map((m) => el('option', { value: m.id }, m.name)));
  paidBy.value = me.id;
  const type = el('select', {}, ...[['equal', 'Equally'], ['exact', 'Exact amounts'], ['percentage', 'Percentages'], ['shares', 'Shares']].map(([v, t]) => el('option', { value: v }, t)));
  const hint = el('div', { class: 'hint' });
  const rows = group.members.map((m) => ({
    m,
    on: el('input', { type: 'checkbox', checked: true }),
    val: el('input', { type: 'text', inputmode: 'decimal', value: '', placeholder: '' }),
  }));
  const grid = el('div', { class: 'splits' });
  for (const r of rows) grid.append(r.on, el('span', {}, r.m.name), r.val);
  const submit = el('button', { class: 'primary', type: 'submit' }, 'Add expense');
  let key = null;

  const placeholder = () => ({ equal: '', exact: '0.00', percentage: '%', shares: '1' })[type.value];
  function refreshHint() {
    for (const r of rows) { r.val.hidden = type.value === 'equal'; r.val.placeholder = placeholder(); }
    hint.className = 'hint';
    if (type.value === 'exact') {
      const total = parseMinor(amount.value, exponentOf(cur.value));
      const sum = rows.filter((r) => r.on.checked).reduce((s, r) => s + (parseMinor(r.val.value, exponentOf(cur.value)) ?? 0), 0);
      if (total !== null) {
        const left = total - sum;
        hint.textContent = left === 0 ? 'Amounts add up.' : `${formatMinor(left, exponentOf(cur.value))} ${cur.value} still to assign.`;
        if (left !== 0) hint.classList.add('bad');
      } else hint.textContent = '';
    } else if (type.value === 'percentage') {
      const sum = rows.filter((r) => r.on.checked).reduce((s, r) => s + (parseMinor(r.val.value, 2) ?? 0), 0);
      hint.textContent = `Total ${formatMinor(sum, 2)}% (must be 100).`;
      if (sum !== 10000) hint.classList.add('bad');
    } else if (type.value === 'shares') hint.textContent = 'Someone with 2 shares pays twice what someone with 1 pays.';
    else hint.textContent = 'Split equally between ticked people. Odd cents are assigned deterministically.';
  }

  const form = el('form', {
    oninput: () => { key = null; refreshHint(); }, // any edit is a new request, so it gets a new key
    onchange: () => { key = null; refreshHint(); },
    onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = '';
      const exp = exponentOf(cur.value);
      const total = parseMinor(amount.value, exp);
      if (!total) return (err.textContent = exp === 0 ? 'Enter a whole-number amount.' : `Enter an amount with at most ${exp} decimals.`);
      const splits = [];
      for (const r of rows.filter((x) => x.on.checked)) {
        if (type.value === 'equal') splits.push({ user_id: r.m.id });
        else {
          const v = type.value === 'exact' ? parseMinor(r.val.value, exp) : type.value === 'percentage' ? parseMinor(r.val.value, 2) : /^\d{1,4}$/.test(r.val.value.trim()) ? Number(r.val.value) : null;
          if (!v) return (err.textContent = `Enter a value for ${r.m.name}, or untick them.`);
          splits.push({ user_id: r.m.id, value: v });
        }
      }
      if (!splits.length) return (err.textContent = 'Pick at least one person.');
      const body = { description: desc.value, amount_minor: total, currency: cur.value, paid_by: Number(paidBy.value), split_type: type.value, splits };
      key = key ?? newKey(); // a retry of the same submit reuses the key, so it can't be recorded twice
      submit.disabled = true;
      const r = await api('POST', `/api/groups/${group.id}/expenses`, body, { 'Idempotency-Key': key });
      submit.disabled = false;
      if (!r.ok) return (err.textContent = errText(r));
      key = null;
      desc.value = ''; amount.value = '';
      for (const x of rows) { x.val.value = ''; x.on.checked = true; }
      refreshHint();
      toast(r.replay ? 'Already recorded' : 'Expense added');
      onDone();
    },
  },
  el('h2', {}, 'Add expense'),
  el('label', {}, 'What for?'), desc,
  el('div', { class: 'row' }, el('div', { class: 'grow' }, el('label', {}, 'Amount'), amount), el('div', {}, el('label', {}, 'Currency'), cur)),
  el('label', {}, 'Paid by'), paidBy,
  el('label', {}, 'Split'), type,
  grid, hint,
  submit, err);
  refreshHint();
  return form;
}

// ---- router ------------------------------------------------------------------

async function route() {
  if (!Object.keys(CURRENCIES).length) {
    const c = await api('GET', '/api/currencies');
    if (c.ok) CURRENCIES = Object.fromEntries(c.data.currencies.map((x) => [x.code, x.exponent]));
  }
  const me = await api('GET', '/api/me');
  if (!me.ok) return viewAuth();
  if (!Object.keys(CURRENCIES).length) {
    const c = await api('GET', '/api/currencies');
    CURRENCIES = Object.fromEntries(c.data.currencies.map((x) => [x.code, x.exponent]));
  }
  const m = /^#\/g\/(\d+)$/.exec(location.hash);
  return m ? viewGroup(me.data, Number(m[1])) : viewHome(me.data);
}

window.addEventListener('hashchange', route);
route();
