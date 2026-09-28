import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const API = 'http://127.0.0.1:8000';
const blank = {
  service: 'payments-api',
  environment: 'production',
  version: 'v1.1',
  deployment_type: 'database migration',
  changes: 'payments.sql',
  description: 'Follow-up migration',
};
const SCENARIOS = [
  'success',
  'database_migration_failure',
  'connection_pool_exhausted',
  'missing_environment_variable',
  'dependency_conflict',
  'health_check_failure',
];

const req = async (path, opts = {}) => {
  let r;
  try {
    r = await fetch(API + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  } catch {
    throw new Error('Cannot reach the backend on port 8000. Is it running?');
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const d = body.detail;
    throw new Error(typeof d === 'string' ? d : 'Request failed (' + r.status + ')');
  }
  return r.json();
};

const MEMORY_LABEL = {
  hindsight_retained: 'Hindsight: retained',
  hindsight_unavailable: 'Hindsight: unavailable',
  local_history_only: 'Local only',
  not_attempted: 'Not recorded',
};

function App() {
  const [deps, setDeps] = useState([]);
  const [form, setForm] = useState(blank);
  const [sel, setSel] = useState();
  const [analysis, setAnalysis] = useState();
  const [notice, setNotice] = useState('');
  const [hs, setHs] = useState(null);
  const [customReason, setCustomReason] = useState('');
  const [busy, setBusy] = useState('');

  const load = () =>
    req('/deployments').then(setDeps).catch((e) => setNotice(e.message));
  const checkHs = () => {
    setHs(null);
    req('/health/hindsight').then(setHs).catch((e) => setHs({ configured: false, reachable: false, error: e.message }));
  };
  useEffect(() => {
    load();
    checkHs();
  }, []);

  const guard = (label, fn) => async (...args) => {
    setBusy(label);
    try {
      await fn(...args);
    } catch (e) {
      setNotice(e.message);
    }
    setBusy('');
  };

  const add = guard('Creating deployment...', async (e) => {
    e.preventDefault();
    const x = await req('/deployments', { method: 'POST', body: JSON.stringify(form) });
    setSel(x);
    setAnalysis();
    setNotice('Deployment #' + x.id + ' created. Analyze it, then pick a simulator outcome.');
    await load();
  });

  const seed = guard('Loading demo...', async () => {
    const r = await req('/demo/seed', { method: 'POST' });
    setNotice(r.message);
    await load();
  });

  const analyze = guard('Recalling from Hindsight...', async () => {
    setAnalysis(await req('/deployments/' + sel.id + '/analyze', { method: 'POST' }));
  });

  const run = guard('Running simulator and saving memory...', async (scenario) => {
    const q = await req('/deployments/' + sel.id + '/execute', {
      method: 'POST',
      body: JSON.stringify({ scenario, failure_reason: customReason }),
    });
    setSel(q);
    const m = q.memory_status;
    setNotice(
      'Simulator result: ' + q.status + '. ' +
        (m === 'hindsight_retained'
          ? 'Retained in Hindsight (background indexing can take up to a minute before it is recallable).'
          : m === 'hindsight_unavailable'
          ? 'Hindsight did not accept the memory (' + (q.memory_error || 'unknown error') + '). Saved locally - use "Sync to Hindsight" once it recovers.'
          : 'Saved as local history; Hindsight is not configured.')
    );
    await load();
  });

  const sync = guard('Syncing to Hindsight...', async () => {
    const r = await req('/memory/sync', { method: 'POST' });
    setNotice(r.message);
    await load();
    checkHs();
  });

  const unsynced = deps.filter((x) => x.status !== 'PENDING' && x.memory_status !== 'hindsight_retained').length;
  const hsClass = hs === null ? 'warn' : hs.reachable ? 'ok' : 'bad';
  const hsText =
    hs === null ? 'Hindsight: checking...'
    : hs.reachable ? 'Hindsight: connected'
    : hs.configured ? 'Hindsight: unreachable'
    : 'Hindsight: not configured';

  return (
    <main>
      <header>
        <div>
          <small>DEVOPS MEMORY</small>
          <h1>Deployment Memory Agent</h1>
        </div>
        <span className={'pill ' + hsClass} title={hs && hs.error ? hs.error : ''} onClick={checkHs}>{hsText}</span>
        <button onClick={seed}>Load demo failure</button>
      </header>

      {hs && hs.error && hs.configured && <p className="notice bad">Hindsight error: {hs.error}. The app still works from local history; click the status pill to re-check.</p>}
      {busy && <p className="notice">{busy}</p>}
      {notice && <p className="notice">{notice}</p>}

      <section className="stats">
        <b>{deps.length}<small> Deployments</small></b>
        <b>{deps.filter((x) => x.status === 'SUCCESS').length}<small> Success</small></b>
        <b>{deps.filter((x) => x.status === 'FAILED').length}<small> Failed</small></b>
        <b>{deps.filter((x) => x.memory_status === 'hindsight_retained').length}<small> In Hindsight</small></b>
      </section>

      <div className="grid">
        <section>
          <h2>New simulated deployment</h2>
          <form onSubmit={add}>
            {Object.entries(form).map(([k, v]) => (
              <label key={k}>
                {k.replace('_', ' ')}
                <input value={v} onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
              </label>
            ))}
            <button disabled={!!busy}>Create deployment</button>
          </form>
        </section>

        <section>
          <h2>Memory analysis</h2>
          {sel ? (
            <>
              <p><b>#{sel.id} {sel.service}</b> / {sel.environment} / {sel.version} <span className={sel.status}>{sel.status}</span></p>
              <button disabled={!!busy} onClick={analyze}>Analyze deployment</button>

              {analysis && (
                <article>
                  <strong>Risk: {analysis.risk_level}</strong>
                  <p><b>Memory source: {analysis.memory_source}</b></p>
                  <p className="muted">{analysis.memory_notice}</p>

                  {analysis.hindsight_memories.length > 0 && (
                    <>
                      <h4>Relevant previous experience (Hindsight)</h4>
                      {analysis.hindsight_memories.map((m, i) => (
                        <p className="incident hs" key={'h' + i}>{m.text}</p>
                      ))}
                    </>
                  )}

                  {analysis.memories.length > 0 && (
                    <>
                      <h4>Local deployment records</h4>
                      {analysis.memories.map((x) => (
                        <p className="incident" key={x.id}>
                          <b>#{x.id} {x.version} {x.status}</b><br />{x.cause}<br />{x.fix}
                        </p>
                      ))}
                    </>
                  )}

                  <h4>Recommendation</h4>
                  <ol>{analysis.recommendations.map((x, i) => <li key={i}>{x}</li>)}</ol>
                </article>
              )}

              <h3>Deployment Simulator</h3>
              {SCENARIOS.map((x) => (
                <button key={x} className="scenario" disabled={!!busy} onClick={() => run(x)}>
                  {x.replaceAll('_', ' ')}
                </button>
              ))}
              <label>
                Or fail with your own reason
                <input
                  placeholder="e.g. Redis timeout during boot"
                  value={customReason}
                  onChange={(e) => setCustomReason(e.target.value)}
                />
              </label>
              <button className="scenario" disabled={!!busy || customReason.trim().length < 3} onClick={() => run('custom_failure')}>
                fail with custom reason
              </button>
            </>
          ) : (
            <p>Create a deployment, then analyze similar incidents before choosing a simulator outcome.</p>
          )}
        </section>
      </div>

      <section>
        <h2>
          Deployment history
          {unsynced > 0 && <button className="sync" disabled={!!busy} onClick={sync}>Sync {unsynced} to Hindsight</button>}
        </h2>
        {deps.map((x) => (
          <article className="history" key={x.id} onClick={() => { setSel(x); setAnalysis(); }}>
            <b>#{x.id} {x.service}</b>
            <span>{x.environment} · {x.version}</span>
            <strong className={x.status}>{x.status}</strong>
            <p>
              {x.cause || 'Pending - choose a simulator outcome'}
              <br />
              <span className={'badge ' + x.memory_status} title={x.memory_error || ''}>{MEMORY_LABEL[x.memory_status] || x.memory_status}</span>
            </p>
          </article>
        ))}
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
