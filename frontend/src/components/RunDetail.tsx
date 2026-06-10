import { useState, useCallback, useRef, useMemo } from 'react';
import { TestRun, fetchRunDetails, fetchStepResults } from '../api';
import AnsiToHtml from 'ansi-to-html';

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

function statusIcon(status: string) {
  switch (status) {
    case 'passed': return '✅';
    case 'failed': return '❌';
    case 'skipped': return '⏭️';
    case 'cancelled': return '🚫';
    default: return '⏳';
  }
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

function stripAnsi(str: string): string {
  return str.replace(/\x1b\[[0-9;]*m/g, '');
}

function generateMarkdown(run: TestRun, results: TestResult[], logs: string[]): string {
  const summary = run.summary ? JSON.parse(run.summary) : null;
  const total = summary ? summary.passed + summary.failed + summary.skipped : results.length;

  let md = `# .NET SDK Test Run Report\n\n`;
  md += `**Run ID:** ${run.id}\n`;
  if (run.sdk_version) md += `**SDK Version:** ${run.sdk_version}\n`;
  md += `**Started:** ${formatTime(run.started_at)}\n`;
  if (run.finished_at) md += `**Finished:** ${formatTime(run.finished_at)}\n`;
  md += `\n`;

  if (summary) {
    md += `## Summary\n\n`;
    md += `| Total | Passed | Failed | Skipped |\n`;
    md += `|-------|--------|--------|--------|\n`;
    md += `| ${total} | ✅ ${summary.passed} | ❌ ${summary.failed} | ⏭️ ${summary.skipped} |\n\n`;
  }

  md += `## Test Results\n\n`;
  md += `| # | Test | Category | Status |\n`;
  md += `|---|------|----------|--------|\n`;
  results.forEach((result, i) => {
    const icon = result.status === 'passed' ? '✅' : result.status === 'failed' ? '❌' : result.status === 'cancelled' ? '⚠️' : '○';
    md += `| ${i + 1} | ${result.title} | ${result.category} | ${icon} ${result.status} |\n`;
  });

  md += `\n## Full Log Output\n\n`;
  md += `\`\`\`\n`;
  logs.forEach(line => {
    md += stripAnsi(line) + '\n';
  });
  md += `\`\`\`\n`;

  return md;
}

async function downloadMarkdown(content: string, run: TestRun) {
  const filename = `test-run-${run.sdk_version || run.id}-${new Date().toISOString().slice(0, 10)}.md`;
  try {
    const res = await fetch('/api/save-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, filename }),
    });
    const data = await res.json();
    if (!data.saved && data.reason !== 'cancelled') {
      blobDownload(content, filename);
    }
  } catch {
    blobDownload(content, filename);
  }
}

function blobDownload(content: string, filename: string) {
  const blob = new Blob([content], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

const ansiConverter = new AnsiToHtml({
  fg: '#eee',
  bg: 'transparent',
  newline: false,
  escapeXML: true,
  colors: {
    0: '#555',
    1: '#f44336',
    2: '#4caf50',
    3: '#ff9800',
    4: '#2196f3',
    5: '#e91e63',
    6: '#00bcd4',
    7: '#eee',
  }
});

type Tab = 'results' | 'logs';

export default function RunDetail({ run, results, tests, onBack }: RunDetailProps) {
  const summary = run.summary ? JSON.parse(run.summary) : null;
  const [tab, setTab] = useState<Tab>('results');
  const [logs, setLogs] = useState<string[] | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const logContainerRef = useRef<HTMLDivElement>(null);

  const loadLogs = useCallback(async () => {
    if (logs !== null) return;
    setLogsLoading(true);
    try {
      const { results: rawResults } = await fetchRunDetails(run.id);
      const reconstructedLogs: string[] = [];

      reconstructedLogs.push(`▶ Run started (${rawResults.length} tests)`);

      for (const result of rawResults as any[]) {
        const test = tests.find(t => t.id === result.test_case_id);
        const title = test?.title || result.test_case_id;
        reconstructedLogs.push(`\n━━━ ${title} ━━━`);

        const steps = await fetchStepResults(run.id, result.id) as any[];
        for (const step of steps) {
          if (step.command) {
            reconstructedLogs.push(`$ ${step.command}`);
          }
          if (step.stdout) {
            const lines = step.stdout.split('\n').filter((l: string) => l);
            reconstructedLogs.push(...lines);
          }
          if (step.stderr) {
            const lines = step.stderr.split('\n').filter((l: string) => l);
            reconstructedLogs.push(...lines.map((l: string) => `[STDERR] ${l}`));
          }
        }

        reconstructedLogs.push(`  ${result.status === 'passed' ? '✅ PASSED' : '❌ FAILED'}`);
      }

      const parsedSummary = run.summary ? JSON.parse(run.summary) : null;
      if (parsedSummary) {
        reconstructedLogs.push(`\n✅ Run complete: ${JSON.stringify(parsedSummary)}`);
      }

      setLogs(reconstructedLogs);
    } finally {
      setLogsLoading(false);
    }
  }, [run, tests, logs]);

  const scrollToTest = useCallback((title: string) => {
    if (!logContainerRef.current || !logs) return;
    const lines = logContainerRef.current.querySelectorAll('.log-line');
    for (let i = 0; i < lines.length; i++) {
      if (logs[i]?.includes(`━━━ ${title} ━━━`)) {
        const line = lines[i] as HTMLElement;
        logContainerRef.current.scrollTop = line.offsetTop - logContainerRef.current.offsetTop;
        break;
      }
    }
  }, [logs]);

  const handleResultRowClick = useCallback(async (result: TestResult) => {
    // Switch to logs tab and scroll to the test
    if (logs === null) {
      setLogsLoading(true);
      setTab('logs');
      // Need to load logs first, then scroll after render
      await loadLogs();
      // Use setTimeout to let the DOM render
      setTimeout(() => scrollToTest(result.title), 100);
    } else {
      setTab('logs');
      setTimeout(() => scrollToTest(result.title), 50);
    }
  }, [logs, loadLogs, scrollToTest]);

  const handleTabChange = (newTab: Tab) => {
    setTab(newTab);
    if (newTab === 'logs') {
      loadLogs();
    }
  };

  const handleExport = useCallback(async () => {
    let logsToExport = logs;
    if (!logsToExport) {
      // Load logs if not already loaded
      await loadLogs();
      // We need to wait for state update, use a workaround
      const { results: rawResults } = await fetchRunDetails(run.id);
      const reconstructedLogs: string[] = [];
      reconstructedLogs.push(`▶ Run started (${rawResults.length} tests)`);
      for (const result of rawResults as any[]) {
        const test = tests.find(t => t.id === result.test_case_id);
        const title = test?.title || result.test_case_id;
        reconstructedLogs.push(`\n━━━ ${title} ━━━`);
        const steps = await fetchStepResults(run.id, result.id) as any[];
        for (const step of steps) {
          if (step.command) reconstructedLogs.push(`$ ${step.command}`);
          if (step.stdout) reconstructedLogs.push(...step.stdout.split('\n').filter((l: string) => l));
          if (step.stderr) reconstructedLogs.push(...step.stderr.split('\n').filter((l: string) => l).map((l: string) => `[STDERR] ${l}`));
        }
        reconstructedLogs.push(`  ${result.status === 'passed' ? '✅ PASSED' : '❌ FAILED'}`);
      }
      logsToExport = reconstructedLogs;
    }
    const md = generateMarkdown(run, results, logsToExport);
    await downloadMarkdown(md, run);
  }, [logs, loadLogs, run, results, tests]);

  const renderedLines = useMemo(() => {
    if (!logs) return [];
    return logs.map((line) => ansiConverter.toHtml(line));
  }, [logs]);

  const total = summary ? summary.passed + summary.failed + summary.skipped : results.length;

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
      </div>

      {summary && (
        <div className="summary-cards">
          <div className="card card-passed">
            <div className="card-value">{summary.passed}</div>
            <div className="card-label">Passed</div>
          </div>
          <div className="card card-failed">
            <div className="card-value">{summary.failed}</div>
            <div className="card-label">Failed</div>
          </div>
          <div className="card card-skipped">
            <div className="card-value">{summary.skipped}</div>
            <div className="card-label">Skipped</div>
          </div>
          <div className="card card-total">
            <div className="card-value">{total}</div>
            <div className="card-label">Total</div>
          </div>
        </div>
      )}

      {summary && total > 0 && (
        <div className="progress-bar">
          <div className="bar-passed" style={{ width: `${(summary.passed / total) * 100}%` }} />
          <div className="bar-failed" style={{ width: `${(summary.failed / total) * 100}%` }} />
          <div className="bar-skipped" style={{ width: `${(summary.skipped / total) * 100}%` }} />
        </div>
      )}

      <div className="run-detail-tabs">
        <button className={tab === 'results' ? 'active' : ''} onClick={() => handleTabChange('results')}>
          Results
        </button>
        <button className={tab === 'logs' ? 'active' : ''} onClick={() => handleTabChange('logs')}>
          Full Log
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
                onClick={() => handleResultRowClick(result)}
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
                  onClick={() => scrollToTest(result.title)}
                >
                  <span className="status-icon">
                    {result.status === 'passed' && '✓'}
                    {result.status === 'failed' && '✗'}
                    {result.status === 'skipped' && '—'}
                    {result.status === 'cancelled' && '—'}
                    {result.status === 'pending' && '○'}
                  </span>
                  <span>{result.title}</span>
                </div>
              ))}
            </div>
            <div className="run-detail-logs" ref={logContainerRef}>
              {logsLoading ? (
                <p className="loading-text">Loading logs...</p>
              ) : logs ? (
                <pre>
                  {renderedLines.map((html, i) => (
                    <div
                      key={i}
                      className={`log-line ${logs[i]?.includes('[STDERR]') ? 'stderr' : ''} ${logs[i]?.startsWith('$ ') ? 'command' : ''}`}
                      dangerouslySetInnerHTML={{ __html: html }}
                    />
                  ))}
                </pre>
              ) : null}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
