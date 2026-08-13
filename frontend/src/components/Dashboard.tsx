import { useMemo, useState } from 'react';
import { TestRun } from '../api';
import { formatTime } from '../report';

interface DashboardProps {
  runs: TestRun[];
  onViewRun: (run: TestRun) => void;
  onRetryRun: (run: TestRun) => void;
  isRunning: boolean;
}

const PAGE_SIZES = [10, 25, 50, 100];

/** `started_at` is a local ISO timestamp, so its date part compares directly
 * against the `YYYY-MM-DD` value of an `<input type="date">`. */
function datePart(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '';
}

export interface HistoryFilters {
  sdkVersion: string;
  status: string;
  from: string;
  to: string;
}

/** Apply the history filters. An empty filter value means "no constraint". */
export function filterRuns(runs: TestRun[], f: HistoryFilters): TestRun[] {
  return runs.filter(run => {
    if (f.sdkVersion && (run.sdk_version || '') !== f.sdkVersion) return false;
    if (f.status && run.status !== f.status) return false;
    const day = datePart(run.started_at);
    if (f.from && (!day || day < f.from)) return false;
    if (f.to && (!day || day > f.to)) return false;
    return true;
  });
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
  const [sdkFilter, setSdkFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [pageSize, setPageSize] = useState<number>(25);
  const [page, setPage] = useState(1);

  // Options come from the full history, so a filter never hides its own value.
  const sdkVersions = useMemo(
    () => Array.from(new Set(runs.map(r => r.sdk_version).filter(Boolean) as string[])).sort(),
    [runs]
  );
  const statuses = useMemo(
    () => Array.from(new Set(runs.map(r => r.status).filter(Boolean))).sort(),
    [runs]
  );

  const filtered = useMemo(
    () => filterRuns(runs, { sdkVersion: sdkFilter, status: statusFilter, from: fromDate, to: toDate }),
    [runs, sdkFilter, statusFilter, fromDate, toDate]
  );

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  // Clamp rather than reset: filtering down mid-browse must not land on a blank page.
  const currentPage = Math.min(page, totalPages);
  const start = (currentPage - 1) * pageSize;
  const visible = filtered.slice(start, start + pageSize);

  const filtersActive = Boolean(sdkFilter || statusFilter || fromDate || toDate);
  const clearFilters = () => {
    setSdkFilter('');
    setStatusFilter('');
    setFromDate('');
    setToDate('');
    setPage(1);
  };

  return (
    <div className="dashboard-view">
      <h2>Run History</h2>
      {runs.length === 0 ? (
        <p className="empty-state">No test runs yet. Select tests and run them to see results here.</p>
      ) : (
        <>
          <div className="history-filters">
            <label>
              SDK Version
              <select value={sdkFilter} onChange={e => { setSdkFilter(e.target.value); setPage(1); }}>
                <option value="">All</option>
                {sdkVersions.map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </label>
            <label>
              Status
              <select value={statusFilter} onChange={e => { setStatusFilter(e.target.value); setPage(1); }}>
                <option value="">All</option>
                {statuses.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label>
              From
              <input type="date" value={fromDate} max={toDate || undefined}
                onChange={e => { setFromDate(e.target.value); setPage(1); }} />
            </label>
            <label>
              To
              <input type="date" value={toDate} min={fromDate || undefined}
                onChange={e => { setToDate(e.target.value); setPage(1); }} />
            </label>
            <button className="clear-filters-btn" onClick={clearFilters} disabled={!filtersActive}>
              Clear filters
            </button>
            <label className="page-size">
              Rows
              <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}>
                {PAGE_SIZES.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          </div>

          {filtered.length === 0 ? (
            <p className="empty-state">No runs match the current filters.</p>
          ) : (
            <>
              <table>
                <thead>
                  <tr><th>SDK Version</th><th>Started</th><th>Tests</th><th>Status</th><th>Summary</th><th>Actions</th></tr>
                </thead>
                <tbody>
                  {visible.map(run => {
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

              <div className="history-pagination">
                <span className="pagination-info">
                  Showing {start + 1}–{start + visible.length} of {filtered.length}
                  {filtered.length !== runs.length && ` (filtered from ${runs.length})`}
                </span>
                <div className="pagination-controls">
                  <button onClick={() => setPage(1)} disabled={currentPage === 1}>« First</button>
                  <button onClick={() => setPage(currentPage - 1)} disabled={currentPage === 1}>‹ Prev</button>
                  <span className="pagination-page">Page {currentPage} of {totalPages}</span>
                  <button onClick={() => setPage(currentPage + 1)} disabled={currentPage === totalPages}>Next ›</button>
                  <button onClick={() => setPage(totalPages)} disabled={currentPage === totalPages}>Last »</button>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
