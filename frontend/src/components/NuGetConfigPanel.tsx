import { useState, useEffect, useCallback } from 'react'
import { NuGetConfigState, NuGetFeed, fetchNuGetConfig, addNuGetFeeds } from '../api'

interface Props {
  onClose: () => void;
}

/**
 * Inspect and repair the machine's user-level NuGet.Config (issue #64).
 *
 * Without the required package sources, `dotnet restore` fails with NU1102 and
 * the run reports a misleading test failure, so missing feeds are surfaced here
 * and can be added in one click. There is a single config file per machine but
 * the feeds a build needs depend on the SDK, so each feed shows what it is for.
 */
export default function NuGetConfigPanel({ onClose }: Props) {
  const [state, setState] = useState<NuGetConfigState | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [customKey, setCustomKey] = useState('');
  const [customUrl, setCustomUrl] = useState('');

  const load = useCallback(async () => {
    setError(null);
    try {
      const s = await fetchNuGetConfig();
      setState(s);
      // Pre-tick exactly what needs fixing, so the common case is one click.
      const next: Record<string, boolean> = {};
      for (const f of s.required) if (!f.present || f.disabled) next[f.key] = true;
      setSelected(next);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const apply = async (feeds: NuGetFeed[]) => {
    if (!feeds.length) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const s = await addNuGetFeeds(feeds);
      if (s.ok === false) {
        setError(s.error || 'Could not update NuGet.Config.');
        return;
      }
      setState(s);
      const bits: string[] = [];
      if (s.added?.length) bits.push(`added ${s.added.join(', ')}`);
      if (s.enabled?.length) bits.push(`re-enabled ${s.enabled.join(', ')}`);
      setResult(bits.length ? `Saved — ${bits.join('; ')}.` : 'No changes were needed.');
      const next: Record<string, boolean> = {};
      for (const f of s.required) if (!f.present || f.disabled) next[f.key] = true;
      setSelected(next);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const addSelected = () => {
    const feeds = (state?.required || []).filter(f => selected[f.key]);
    apply(feeds);
  };

  const addCustom = () => {
    const key = customKey.trim();
    const url = customUrl.trim();
    if (!key || !url) return;
    apply([{ key, url, protocol_version: '3' }]).then(() => {
      setCustomKey('');
      setCustomUrl('');
    });
  };

  const pending = (state?.required || []).filter(f => selected[f.key]).length;

  return (
    <div className="modal-overlay">
      <div className="modal nuget-modal">
        <h3>📦 NuGet.Config</h3>
        <p className="nuget-path">
          {state ? state.path : 'Loading…'}
        </p>

        {state && !state.exists && (
          <p className="nuget-warn">
            This file doesn't exist yet. NuGet only creates it after the SDK has been
            used once — run <code>dotnet --info</code> (or just add a feed below, which
            creates it).
          </p>
        )}
        {state?.error && <p className="modal-error">{state.error}</p>}
        {error && <p className="modal-error">{error}</p>}
        {result && <p className="nuget-ok">{result}</p>}

        <h4>Required feeds</h4>
        <p className="nuget-hint">
          Missing feeds cause <code>NU1102</code> errors, which show up as failed tests.
        </p>
        <ul className="nuget-list">
          {(state?.required || []).map(f => {
            const ok = f.present && !f.disabled;
            return (
              <li key={f.key}>
                <label>
                  <input
                    type="checkbox"
                    checked={!!selected[f.key]}
                    disabled={busy || ok}
                    onChange={e => setSelected(s => ({ ...s, [f.key]: e.target.checked }))}
                  />
                  <span className={ok ? 'nuget-state-ok' : 'nuget-state-missing'}>
                    {ok ? '✓' : '✗'}
                  </span>
                  <span className="nuget-key">{f.key}</span>
                  {f.sdk && <span className="nuget-sdk">for {f.sdk}</span>}
                  {f.present && f.disabled && <span className="nuget-sdk">configured but disabled</span>}
                </label>
                <div className="nuget-url">{f.url}</div>
              </li>
            );
          })}
        </ul>

        <h4>Currently configured sources</h4>
        {state && state.sources.length === 0 ? (
          <p className="nuget-hint">None.</p>
        ) : (
          <ul className="nuget-list">
            {(state?.sources || []).map(s => (
              <li key={s.key}>
                <span className="nuget-key">{s.key}</span>
                {s.disabled && <span className="nuget-sdk">disabled</span>}
                <div className="nuget-url">{s.url}</div>
              </li>
            ))}
          </ul>
        )}

        <h4>Add another feed</h4>
        <div className="nuget-custom">
          <input
            placeholder="name, e.g. dotnet11"
            value={customKey}
            disabled={busy}
            onChange={e => setCustomKey(e.target.value)}
          />
          <input
            placeholder="https://…/nuget/v3/index.json"
            value={customUrl}
            disabled={busy}
            onChange={e => setCustomUrl(e.target.value)}
          />
          <button onClick={addCustom} disabled={busy || !customKey.trim() || !customUrl.trim()}>
            Add
          </button>
        </div>

        <div className="modal-actions">
          <button onClick={onClose} disabled={busy}>Close</button>
          <button onClick={load} disabled={busy}>↻ Reload</button>
          <button className="run-btn" onClick={addSelected} disabled={busy || pending === 0}>
            {busy ? '⏳ Saving…' : `Add ${pending} selected feed${pending === 1 ? '' : 's'}`}
          </button>
        </div>
        <p className="nuget-hint">A backup is written to NuGet.Config.bak before any change.</p>
      </div>
    </div>
  );
}
