import { ReactNode } from 'react'
import { TestCase } from '../api'

interface Props {
  status: string;
  testStatuses: Record<string, string>;
  tests: TestCase[];
  onCancel: () => void;
  onTestClick?: (testTitle: string) => void;
  children: ReactNode;
}

export default function TestRunner({ status, testStatuses, tests, onCancel, onTestClick, children }: Props) {
  return (
    <div className="runner">
      <div className="runner-header">
        <h2>
          {status === 'running' && '🔄 Running Tests...'}
          {status === 'completed' && '✅ Run Complete'}
          {status === 'cancelled' && '⚠️ Run Cancelled'}
          {status === 'idle' && '⏸ Idle'}
        </h2>
        {status === 'running' && (
          <button className="cancel-btn" onClick={onCancel}>✕ Cancel</button>
        )}
      </div>

      <div className="test-progress">
        {tests.map(test => {
          const s = testStatuses[test.id] || 'pending';
          return (
            <div
              key={test.id}
              className={`progress-item status-${s} ${s !== 'pending' ? 'clickable' : ''}`}
              onClick={() => s !== 'pending' && onTestClick?.(test.title)}
            >
              <span className="status-icon">
                {s === 'pending' && '○'}
                {s === 'running' && '◉'}
                {s === 'passed' && '✓'}
                {s === 'passed_with_warnings' && '⚠'}
                {s === 'failed' && '✗'}
                {s === 'skipped' && '⏭'}
                {s === 'cancelled' && '—'}
              </span>
              <span>{test.title}</span>
            </div>
          );
        })}
      </div>

      {children}
    </div>
  );
}
