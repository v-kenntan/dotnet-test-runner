import { Summary, generateMarkdown, downloadMarkdown } from '../report';
import SummaryCards from './SummaryCards';

interface Props {
  summary: Summary;
  logs: string[];
  testStatuses: Record<string, string>;
  tests: { id: string; title: string }[];
}

export default function ResultsSummary({ summary, logs, testStatuses, tests }: Props) {
  const handleExport = async () => {
    const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
    const rows = tests.map(test => ({ title: test.title, status: testStatuses[test.id] || 'pending' }));
    const md = generateMarkdown([['Date', now]], summary, rows, logs);
    await downloadMarkdown(md, `test-run-${new Date().toISOString().slice(0, 10)}.md`);
  };

  return (
    <div className="results-summary">
      <div className="results-header">
        <h3>Results</h3>
        <button className="export-btn" onClick={handleExport}>📄 Export to Markdown</button>
      </div>
      <SummaryCards summary={summary} />
    </div>
  );
}
