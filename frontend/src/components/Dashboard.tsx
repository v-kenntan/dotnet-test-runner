import { TestRun } from '../api';
import { formatTime } from '../report';

interface DashboardProps {
  runs: TestRun[];
  onViewRun: (run: TestRun) => void;
  onRetryRun: (run: TestRun) => void;
  isRunning: boolean;
}

function SummaryBar({ summary }: { summary: string | null }) {
  if (!summary) return <span>-</span>;
  try {
    const data = JSON.parse(summary);
    const warnings = data.warnings ?? 0;
    const total = data.passed + data.failed + data.skipped + warnings;
    if (total === 0) return <span>-</span>;
    // Warnings still count as passing tests for the completion percentage.
    const okPct = Math.round(((data.passed + warnings) / total) * 100);
    const color = data.failed > 0 || data.skipped > 0
      ? (okPct >= 50 ? '#ffc107' : '#dc3545')
      : (warnings > 0 ? '#ffca28' : '#28a745');
    return (
      <div className="summary-bar-container">
        <div className="summary-bar-track">
          <div className="summary-bar-fill" style={{ width: `${okPct}%`, background: color }} />
        </div>
        <span className="summary-bar-label">{okPct}%</span>
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
                  totalTests = String(s.passed + s.failed + s.skipped + (s.warnings ?? 0));
                } catch {}
              }
              return (
                <tr key={run.id} className="history-row clickable" onClick={() => onViewRun(run)}>
                  <td>{run.sdk_version || 'N/A'}</td>
                  <td>{formatTime(run.started_at)}</td>
                  <td>{totalTests}</td>
                  <td><span className={`badge badge-${run.status}`}>{run.status}</span></td>
                  <td><SummaryBar summary={run.summary} /></td>
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
