import { TestRun } from '../api';

interface DashboardProps {
  runs: TestRun[];
  onViewRun: (run: TestRun) => void;
  onRetryRun: (run: TestRun) => void;
  isRunning: boolean;
}

function formatTime(iso: string | null): string {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  } catch {
    return iso;
  }
}

function SummaryBadges({ summary }: { summary: string | null }) {
  if (!summary) return <span>-</span>;
  try {
    const data = JSON.parse(summary);
    return (
      <div className="summary-badges">
        {data.passed > 0 && <span className="summary-badge badge-passed">{data.passed} passed</span>}
        {data.failed > 0 && <span className="summary-badge badge-failed">{data.failed} failed</span>}
        {data.skipped > 0 && <span className="summary-badge badge-skipped">{data.skipped} skipped</span>}
      </div>
    );
  } catch {
    return <span>{summary}</span>;
  }
}

export default function Dashboard({ runs, onViewRun, onRetryRun, isRunning }: DashboardProps) {
  return (
    <div className="dashboard-view">
      <h2>Run History</h2>
      {runs.length === 0 ? (
        <p className="empty-state">No test runs yet. Select tests and run them to see results here.</p>
      ) : (
        <table>
          <thead>
            <tr><th>SDK Version</th><th>Started</th><th>Tests</th><th>Status</th><th>Summary</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {runs.map(run => {
              let totalTests = '-';
              if (run.summary) {
                try {
                  const s = JSON.parse(run.summary);
                  totalTests = String(s.passed + s.failed + s.skipped);
                } catch {}
              }
              return (
                <tr key={run.id} className="history-row clickable" onClick={() => onViewRun(run)}>
                  <td>{run.sdk_version || 'N/A'}</td>
                  <td>{formatTime(run.started_at)}</td>
                  <td>{totalTests}</td>
                  <td><span className={`badge badge-${run.status}`}>{run.status}</span></td>
                  <td><SummaryBadges summary={run.summary} /></td>
                  <td className="actions-cell" onClick={e => e.stopPropagation()}>
                    <button
                      className="retry-btn"
                      onClick={() => onRetryRun(run)}
                      disabled={isRunning}
                      title={isRunning ? 'A run is already in progress' : 'Re-run with same tests and SDK'}
                    >
                      🔄 Retry
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
