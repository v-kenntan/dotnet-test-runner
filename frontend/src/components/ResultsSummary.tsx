interface Props {
  summary: { passed: number; failed: number; skipped: number };
  logs: string[];
  testStatuses: Record<string, string>;
  tests: { id: string; title: string }[];
}

function stripAnsi(str: string): string {
  return str.replace(/\x1b\[[0-9;]*m/g, '');
}

function generateMarkdown(summary: Props['summary'], logs: string[], testStatuses: Record<string, string>, tests: Props['tests']): string {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const total = summary.passed + summary.failed + summary.skipped;

  let md = `# .NET SDK Test Run Report\n\n`;
  md += `**Date:** ${now}\n\n`;
  md += `## Summary\n\n`;
  md += `| Total | Passed | Failed | Skipped |\n`;
  md += `|-------|--------|--------|--------|\n`;
  md += `| ${total} | ✅ ${summary.passed} | ❌ ${summary.failed} | ⏭️ ${summary.skipped} |\n\n`;

  md += `## Test Results\n\n`;
  md += `| # | Test | Status |\n`;
  md += `|---|------|--------|\n`;
  tests.forEach((test, i) => {
    const status = testStatuses[test.id] || 'pending';
    const icon = status === 'passed' ? '✅' : status === 'failed' ? '❌' : status === 'cancelled' ? '⚠️' : '○';
    md += `| ${i + 1} | ${test.title} | ${icon} ${status} |\n`;
  });

  md += `\n## Full Log Output\n\n`;
  md += `\`\`\`\n`;
  logs.forEach(line => {
    md += stripAnsi(line) + '\n';
  });
  md += `\`\`\`\n`;

  return md;
}

async function downloadMarkdown(content: string) {
  const filename = `test-run-${new Date().toISOString().slice(0, 10)}.md`;
  try {
    const res = await fetch('/api/save-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, filename }),
    });
    const data = await res.json();
    if (!data.saved && data.reason !== 'cancelled') {
      // Fallback to blob download (browser mode)
      blobDownload(content, filename);
    }
  } catch {
    // Fallback to blob download if backend unavailable
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

export default function ResultsSummary({ summary, logs, testStatuses, tests }: Props) {
  const total = summary.passed + summary.failed + summary.skipped;

  const handleExport = async () => {
    const md = generateMarkdown(summary, logs, testStatuses, tests);
    await downloadMarkdown(md);
  };

  return (
    <div className="results-summary">
      <div className="results-header">
        <h3>Results</h3>
        <button className="export-btn" onClick={handleExport}>📄 Export to Markdown</button>
      </div>
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
      {total > 0 && (
        <div className="progress-bar">
          <div className="bar-passed" style={{ width: `${(summary.passed / total) * 100}%` }} />
          <div className="bar-failed" style={{ width: `${(summary.failed / total) * 100}%` }} />
          <div className="bar-skipped" style={{ width: `${(summary.skipped / total) * 100}%` }} />
        </div>
      )}
    </div>
  );
}
