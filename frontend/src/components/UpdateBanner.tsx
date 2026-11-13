import { useEffect, useState } from 'react';
import { applyUpdate, fetchUpdateStatus, UpdateStatus } from '../api';

/**
 * Offers the latest GitHub Release when it differs from this build (issue #89).
 * `checkRequest` goes up each time the user clicks "Check for updates"; 0 is the launch check.
 */
export default function UpdateBanner({ isRunning, checkRequest, onVersion }: {
  isRunning: boolean;
  checkRequest: number;
  onVersion: (version: string) => void;
}) {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  // A failed update, the result of a manual check, or update progress.
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const manual = checkRequest > 0;
    if (manual) setNote('Checking for updates...');
    fetchUpdateStatus()
      .then((s) => {
        setStatus(s);
        onVersion(s.version);
        if (!manual) setNote(s.failed ? 'The last update failed. The old version was restored.' : null);
        else if (s.available) setNote(null);
        else if (s.error) setNote(s.error);
        else setNote(s.version === 'dev' ? 'Updates are off in a dev build.' : `You have the latest version (${s.version}).`);
      })
      .catch(() => setNote(manual ? 'Could not reach the app.' : null));
  }, [checkRequest]);

  const available = !busy && status?.available;
  if (!available && !note) return null;

  const update = async () => {
    setBusy(true);
    setNote('Downloading the update...');
    const result = await applyUpdate().catch(() => ({ error: 'Could not reach the app.' }));
    if (result.error) {
      setBusy(false);
      setNote(result.error);
      return;
    }
    setNote('Updating, the app will restart.');
    window.close();
    // Shown only if the browser did not let the page close its own window.
    setTimeout(() => setNote('Updating, the app will restart. You can close this window.'), 2000);
  };

  return (
    <div className="results-destination warn update-banner">
      <span className="results-destination-icon">⬆️</span>
      <span>
        {note}
        {note && available && ' '}
        {available && `Version ${available} is available (you have ${status?.version}).`}
      </span>
      {available && (
        <button className="sdk-refresh-btn" onClick={update} disabled={isRunning} title={isRunning ? 'Wait for the test run to finish.' : undefined}>
          Update
        </button>
      )}
      {!available && !busy && <button className="sdk-refresh-btn" onClick={() => setNote(null)}>Dismiss</button>}
    </div>
  );
}
