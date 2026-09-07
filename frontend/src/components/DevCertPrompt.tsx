import { useState } from 'react'
import { TestCase, trustDevCert } from '../api'

interface Props {
  tests: TestCase[];
  sdkPath?: string;
  onProceed: () => void;
  onCancel: () => void;
}

/**
 * Shown before a run that contains HTTPS tests (test cases 4 and 9) when no
 * trusted ASP.NET Core development certificate exists. Trusting up front keeps
 * the Windows security dialog from interrupting the run midway.
 */
export default function DevCertPrompt({ tests, sdkPath, onProceed, onCancel }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleTrust = async () => {
    setBusy(true);
    setError(null);
    try {
      const status = await trustDevCert(sdkPath);
      if (status.trusted) onProceed();
      else setError(status.output || 'dotnet dev-certs https --trust did not succeed.');
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay">
      <div className="modal">
        <h3>🔒 HTTPS development certificate required</h3>
        <p>
          These tests host HTTPS sites and need a trusted development certificate:
        </p>
        <ul className="modal-list">
          {tests.map(t => <li key={t.id}>{t.title}</li>)}
        </ul>
        <p>
          Trust it now so the Windows security dialog doesn't interrupt the run midway.
          Click <strong>Trust certificate</strong>, then accept the dialog Windows shows.
        </p>
        <code className="modal-code">dotnet dev-certs https --trust</code>
        {error && <p className="modal-error">{error}</p>}
        <div className="modal-actions">
          <button onClick={onCancel} disabled={busy}>Cancel run</button>
          <button onClick={onProceed} disabled={busy}>Run anyway</button>
          <button className="run-btn" onClick={handleTrust} disabled={busy}>
            {busy ? '⏳ Waiting for the security dialog...' : '🔒 Trust certificate'}
          </button>
        </div>
      </div>
    </div>
  );
}
