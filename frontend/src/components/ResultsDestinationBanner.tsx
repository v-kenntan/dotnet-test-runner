import { useEffect, useState } from 'react';
import { ResultsDestination, fetchResultsDestination } from '../api';

/** Where finished runs are exported to.
 *
 * Testers run this on a throwaway VM, so a local-only destination means the
 * results die with the machine. That case is called out loudly rather than
 * left for someone to discover after the VM is gone.
 */
export default function ResultsDestinationBanner() {
  const [dest, setDest] = useState<ResultsDestination | null>(null);

  useEffect(() => {
    fetchResultsDestination().then(setDest).catch(() => setDest(null));
  }, []);

  if (!dest) return null;

  return (
    <div className={`results-destination ${dest.persistent ? 'ok' : 'warn'}`}>
      <span className="results-destination-icon">{dest.persistent ? '📁' : '⚠️'}</span>
      <span>
        {dest.persistent ? 'Results are saved to' : 'Results are only saved locally to'}
        {' '}
        <code>{dest.root}</code>
        {dest.persistent
          ? null
          : ` — they will be lost when this machine is gone. Set ${dest.env_var} to a OneDrive folder or network share.`}
      </span>
    </div>
  );
}
