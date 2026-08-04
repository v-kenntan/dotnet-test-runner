import { Summary, totalOf } from '../report';

/** Passed/warnings/failed/skipped cards plus the proportional progress bar. */
export default function SummaryCards({ summary }: { summary: Summary }) {
  const warnings = summary.warnings ?? 0;
  const total = totalOf(summary);
  const cards: [string, number, string][] = [
    ['passed', summary.passed, 'Passed'],
    ['warnings', warnings, 'Warnings'],
    ['failed', summary.failed, 'Failed'],
    ['skipped', summary.skipped, 'Skipped'],
    ['total', total, 'Total'],
  ];

  return (
    <>
      <div className="summary-cards">
        {cards.map(([key, value, label]) => (
          <div key={key} className={`card card-${key}`}>
            <div className="card-value">{value}</div>
            <div className="card-label">{label}</div>
          </div>
        ))}
      </div>
      {total > 0 && (
        <div className="progress-bar">
          <div className="bar-passed" style={{ width: `${(summary.passed / total) * 100}%` }} />
          <div className="bar-warnings" style={{ width: `${(warnings / total) * 100}%` }} />
          <div className="bar-failed" style={{ width: `${(summary.failed / total) * 100}%` }} />
          <div className="bar-skipped" style={{ width: `${(summary.skipped / total) * 100}%` }} />
        </div>
      )}
    </>
  );
}
