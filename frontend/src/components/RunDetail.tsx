import { useState, useCallback, useRef } from 'react';
import { TestRun, fetchScreenshots, RunScreenshot } from '../api';
import {
  Summary, statusIcon, formatTime, generateMarkdown, downloadMarkdown, reconstructLogs,
} from '../report';
import SummaryCards from './SummaryCards';
import LogViewer, { LogViewerHandle } from './LogViewer';

interface TestResult {
  id: string;
  run_id: string;
  test_case_id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  title: string;
  category: string;
}

interface RunDetailProps {
  run: TestRun;
  results: TestResult[];
  tests: { id: string; title: string }[];
  onBack: () => void;
}

type Tab = 'results' | 'logs' | 'screenshots';

export default function RunDetail({ run, results, tests, onBack }: RunDetailProps) {
  const summary: Summary | null = run.summary ? JSON.parse(run.summary) : null;
  const [tab, setTab] = useState<Tab>('results');
  const [logs, setLogs] = useState<string[] | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const logViewerRef = useRef<LogViewerHandle>(null);
  const [screenshots, setScreenshots] = useState<RunScreenshot[] | null>(null);
  const [screenshotsLoading, setScreenshotsLoading] = useState(false);

  const loadLogs = useCallback(async (): Promise<string[]> => {
    if (logs !== null) return logs;
    setLogsLoading(true);
    try {
      const rebuilt = await reconstructLogs(run, tests);
      setLogs(rebuilt);
      return rebuilt;
    } finally {
      setLogsLoading(false);
    }
  }, [run, tests, logs]);

  const showLogFor = useCallback(async (title: string) => {
    setTab('logs');
    await loadLogs();
    // Let the lines render before scrolling to one.
    setTimeout(() => logViewerRef.current?.scrollToTest(title), 100);
  }, [loadLogs]);

  const loadScreenshots = useCallback(async () => {
    setScreenshotsLoading(true);
    try {
      setScreenshots(await fetchScreenshots(run.id));
    } finally {
      setScreenshotsLoading(false);
    }
  }, [run.id]);

  const handleTabChange = (newTab: Tab) => {
    setTab(newTab);
    if (newTab === 'logs') {
      loadLogs();
    } else if (newTab === 'screenshots') {
      loadScreenshots();
    }
  };

  const handleExport = useCallback(async () => {
    const logsToExport = await loadLogs();
    const meta: [string, string][] = [['Run ID', run.id]];
    if (run.sdk_version) meta.push(['SDK Version', run.sdk_version]);
    meta.push(['Started', formatTime(run.started_at)]);
    if (run.finished_at) meta.push(['Finished', formatTime(run.finished_at)]);
    const md = generateMarkdown(meta, summary, results, logsToExport);
    const date = new Date().toISOString().slice(0, 10);
    await downloadMarkdown(md, `test-run-${run.sdk_version || run.id}-${date}.md`);
  }, [loadLogs, run, results, summary]);

  return (
    <div className="run-detail-view">
      <div className="run-detail-header">
        <button className="back-btn" onClick={onBack}>← Back to Dashboard</button>
        <h2>Run {run.id}</h2>
        <span className={`badge badge-${run.status}`}>{run.status}</span>
      </div>

      <div className="run-detail-meta">
        <span>Started: {formatTime(run.started_at)}</span>
        {run.finished_at && <span>Finished: {formatTime(run.finished_at)}</span>}
        {run.sdk_path && <span title="Pinned SDK install folder for this run">SDK folder: {run.sdk_path}</span>}
      </div>

      {summary && <SummaryCards summary={summary} />}

      <div className="run-detail-tabs">
        <button className={tab === 'results' ? 'active' : ''} onClick={() => handleTabChange('results')}>
          Results
        </button>
        <button className={tab === 'logs' ? 'active' : ''} onClick={() => handleTabChange('logs')}>
          Full Log
        </button>
        <button className={tab === 'screenshots' ? 'active' : ''} onClick={() => handleTabChange('screenshots')}>
          Screenshots
        </button>
      </div>

      {tab === 'results' && (
        <table className="run-detail-table">
          <thead>
            <tr>
              <th>Status</th>
              <th>Test</th>
              <th>Category</th>
              <th>Started</th>
              <th>Finished</th>
            </tr>
          </thead>
          <tbody>
            {results.map(result => (
              <tr
                key={result.id}
                className={`result-row result-${result.status} clickable`}
                onClick={() => showLogFor(result.title)}
                title="Click to view log for this test"
              >
                <td className="result-status">{statusIcon(result.status)} {result.status}</td>
                <td>{result.title}</td>
                <td>{result.category}</td>
                <td>{formatTime(result.started_at)}</td>
                <td>{formatTime(result.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {tab === 'logs' && (
        <>
          <div className="run-detail-log-toolbar">
            <button className="export-btn" onClick={handleExport}>📄 Export to Markdown</button>
          </div>
          <div className="run-detail-logs-container">
            <div className="run-detail-test-sidebar">
              {results.map(result => (
                <div
                  key={result.id}
                  className={`sidebar-item status-${result.status} clickable`}
                  onClick={() => showLogFor(result.title)}
                >
                  <span className="status-icon">
                    {result.status === 'passed' && '✓'}
                    {result.status === 'passed_with_warnings' && '⚠'}
                    {result.status === 'failed' && '✗'}
                    {(result.status === 'skipped' || result.status === 'cancelled') && '—'}
                    {result.status === 'pending' && '○'}
                  </span>
                  <span>{result.title}</span>
                </div>
              ))}
            </div>
            {logsLoading ? (
              <div className="run-detail-logs"><p className="loading-text">Loading logs...</p></div>
            ) : (
              <LogViewer
                ref={logViewerRef}
                logs={logs || []}
                autoScroll={false}
                className="run-detail-logs"
              />
            )}
          </div>
        </>
      )}

      {tab === 'screenshots' && (
        <div className="screenshots-tab">
          {screenshotsLoading ? (
            <p className="loading-text">Loading screenshots...</p>
          ) : !screenshots || screenshots.length === 0 ? (
            <p className="loading-text">No screenshots were captured for this run.</p>
          ) : (
            <div className="screenshots-grid">
              {screenshots.map(shot => (
                <a
                  key={shot.name}
                  className="screenshot-card"
                  href={shot.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`${shot.name} — click to open full size`}
                >
                  <img src={shot.url} alt={shot.name} loading="lazy" />
                  <span className="screenshot-name">{shot.name}</span>
                </a>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
