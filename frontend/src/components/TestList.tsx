import { useEffect, useState } from 'react'
import { TestCase, pickFolder } from '../api'

interface Props {
  categories: Record<string, TestCase[]>;
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  onSelectCategory: (category: string) => void;
  onEdit: (test: TestCase) => void;
  onDelete: (test: TestCase) => void;
  onSdkPathChange: (test: TestCase, sdkPath: string | null) => Promise<void>;
}

/**
 * Inline "SDK folder" picker for tests that are only meaningful against a
 * pinned SDK install (the zip-install workload cases). It writes to the same
 * `sdk_path` field as the editor's own SDK folder box, so the two stay in sync.
 */
function InlineSdkPath({ test, onSdkPathChange }: { test: TestCase; onSdkPathChange: Props['onSdkPathChange'] }) {
  const saved = test.sdk_path || '';
  const [value, setValue] = useState(saved);
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved'>('idle');
  const [error, setError] = useState('');

  // Re-sync when the test is reloaded (e.g. after saving it in the editor).
  // Only the value is reset: clearing `status` here would wipe the "Saved"
  // note the moment our own save refreshes the test.
  useEffect(() => {
    setValue(saved);
  }, [saved]);

  const save = async (next: string) => {
    if (next.trim() === saved.trim()) return;
    setStatus('saving');
    setError('');
    try {
      await onSdkPathChange(test, next.trim() || null);
      setStatus('saved');
    } catch (e) {
      setStatus('idle');
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const handleBrowse = async () => {
    const res = await pickFolder();
    if (!res.picked || !res.path) return;
    setValue(res.path);
    await save(res.path);
    if (res.has_dotnet === false) {
      alert('Selected folder does not contain a dotnet executable. Pick the SDK install root (the folder that contains dotnet.exe).');
    }
  };

  return (
    <div className="test-sdk-path" onClick={e => e.stopPropagation()}>
      <label className="test-sdk-path-label" htmlFor={`sdk-path-${test.id}`}>SDK folder</label>
      <div className="sdk-folder-row">
        <input
          id={`sdk-path-${test.id}`}
          value={value}
          placeholder="e.g. C:\dotnet-zip — folder containing dotnet.exe. Blank = default (PATH) SDK."
          onChange={e => { setValue(e.target.value); setStatus('idle'); setError(''); }}
          onBlur={e => save(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
            if (e.key === 'Escape') { setValue(saved); setError(''); }
          }}
        />
        <button type="button" className="small-btn" onClick={handleBrowse}>📁 Browse</button>
        {value && (
          <button
            type="button"
            className="small-btn"
            title="Clear — use default PATH SDK"
            onClick={() => { setValue(''); save(''); }}
          >✕</button>
        )}
        {status === 'saving' && <span className="test-sdk-path-note">Saving…</span>}
        {status === 'saved' && <span className="test-sdk-path-note ok">✓ Saved</span>}
        {error && <span className="test-sdk-path-note error">⚠ {error}</span>}
      </div>
      {!saved && status !== 'saving' && (
        <small className="test-sdk-path-warn">
          ⚠️ No SDK pinned — this test will run against the machine's default (PATH) SDK.
        </small>
      )}
    </div>
  );
}

export default function TestList({ categories, selectedIds, onToggle, onSelectCategory, onEdit, onDelete, onSdkPathChange }: Props) {
  return (
    <div className="test-list">
      {Object.entries(categories).map(([category, tests]) => {
        const allSelected = tests.every(t => selectedIds.has(t.id));
        return (
          <div key={category} className="category">
            <div className="category-header">
              <h3>{category}</h3>
              <button className="small-btn" onClick={() => onSelectCategory(category)}>
                {allSelected ? 'Deselect All' : 'Select All'}
              </button>
            </div>
            {tests.map(test => (
              <div key={test.id} className={`test-item ${selectedIds.has(test.id) ? 'selected' : ''}`}>
                <label>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(test.id)}
                    onChange={() => onToggle(test.id)}
                  />
                  <span className="test-title">{test.title}</span>
                  {test.is_machine_mutating && <span className="badge badge-warning">⚠️ Machine</span>}
                  {test.is_continuous && <span className="badge badge-info" title="Runs as one continuous sequence: shares a working directory with the adjacent continuous tests, and is skipped if an earlier one fails">⛓ Continuous</span>}
                  {test.workload_version && <span className="badge badge-info" title="Workload set version used by this test">🎯 {test.workload_version}</span>}
                </label>
                <div className="test-actions">
                  <button className="small-btn" onClick={() => onEdit(test)}>Edit</button>
                  {!test.is_builtin && <button className="small-btn btn-danger" onClick={(e) => { e.stopPropagation(); onDelete(test); }}>Delete</button>}
                </div>
                {test.description && <p className="test-desc">{test.description}</p>}
                {test.requires_sdk_path && (
                  <InlineSdkPath test={test} onSdkPathChange={onSdkPathChange} />
                )}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
