import { TestRun, fetchRunDetails, fetchStepResults } from './api';

export interface Summary {
  passed: number;
  failed: number;
  skipped: number;
  warnings?: number;
}

export interface ReportRow {
  title: string;
  category?: string;
  status: string;
}

export function totalOf(summary: Summary): number {
  return summary.passed + summary.failed + summary.skipped + (summary.warnings ?? 0);
}

export function statusIcon(status: string): string {
  switch (status) {
    case 'passed': return '✅';
    case 'passed_with_warnings': return '⚠️';
    case 'failed': return '❌';
    case 'skipped': return '⏭️';
    case 'cancelled': return '🚫';
    default: return '⏳';
  }
}

export function formatTime(iso: string | null): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  } catch {
    return iso;
  }
}

export function stripAnsi(str: string): string {
  return str.replace(/\x1b\[[0-9;]*m/g, '');
}

export function resultLine(status: string): string {
  return `  ${status === 'passed' ? '✅ PASSED' : status === 'passed_with_warnings' ? '⚠️ PASSED (warnings)' : '❌ FAILED'}`;
}

/** Rebuild the run log from the persisted step results of a finished run. */
export async function reconstructLogs(
  run: TestRun,
  tests: { id: string; title: string }[]
): Promise<string[]> {
  const { results } = await fetchRunDetails(run.id);
  const logs: string[] = [`▶ Run started (${results.length} tests)`];

  for (const result of results as any[]) {
    const title = tests.find(t => t.id === result.test_case_id)?.title || result.test_case_id;
    logs.push(`\n━━━ ${title} ━━━`);

    const steps = await fetchStepResults(run.id, result.id) as any[];
    for (const step of steps) {
      if (step.command) logs.push(`$ ${step.command}`);
      if (step.stdout) logs.push(...step.stdout.split('\n').filter((l: string) => l));
      if (step.stderr) {
        logs.push(...step.stderr.split('\n').filter((l: string) => l).map((l: string) => `[STDERR] ${l}`));
      }
    }
    logs.push(resultLine(result.status));
  }

  if (run.summary) logs.push(`\n✅ Run complete: ${run.summary}`);
  return logs;
}

function mdIcon(status: string): string {
  switch (status) {
    case 'passed': return '✅';
    case 'passed_with_warnings': return '⚠️';
    case 'failed': return '❌';
    case 'cancelled': return '⚠️';
    default: return '○';
  }
}

export function generateMarkdown(
  meta: [string, string][],
  summary: Summary | null,
  rows: ReportRow[],
  logs: string[]
): string {
  const withCategory = rows.some(r => r.category !== undefined);

  let md = `# .NET SDK Test Run Report\n\n`;
  meta.forEach(([key, value]) => { md += `**${key}:** ${value}\n`; });
  md += `\n`;

  if (summary) {
    md += `## Summary\n\n`;
    md += `| Total | Passed | Warnings | Failed | Skipped |\n`;
    md += `|-------|--------|----------|--------|--------|\n`;
    md += `| ${totalOf(summary)} | ✅ ${summary.passed} | ⚠️ ${summary.warnings ?? 0} | ❌ ${summary.failed} | ⏭️ ${summary.skipped} |\n\n`;
  }

  md += `## Test Results\n\n`;
  md += withCategory ? `| # | Test | Category | Status |\n|---|------|----------|--------|\n`
                     : `| # | Test | Status |\n|---|------|--------|\n`;
  rows.forEach((row, i) => {
    const cells = withCategory ? `${row.title} | ${row.category || ''}` : row.title;
    md += `| ${i + 1} | ${cells} | ${mdIcon(row.status)} ${row.status} |\n`;
  });

  md += `\n## Full Log Output\n\n\`\`\`console\n`;
  logs.forEach(line => { md += stripAnsi(line) + '\n'; });
  md += `\`\`\`\n`;

  return md;
}

/** Save via the backend's native dialog, falling back to a browser download. */
export async function downloadMarkdown(content: string, filename: string) {
  try {
    const res = await fetch('/api/save-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, filename }),
    });
    const data = await res.json();
    if (data.saved || data.reason === 'cancelled') return;
  } catch {
    // backend unavailable — fall through to the blob download
  }
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
