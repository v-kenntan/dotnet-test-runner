import { useState } from 'react'
import { TestCase, Step, createTest, updateTest, pickFolder } from '../api'

interface Props {
  test: TestCase | null;
  onSave: () => void;
  onCancel: () => void;
}

export default function TestEditor({ test, onSave, onCancel }: Props) {
  const [category, setCategory] = useState(test?.category || '');
  const [title, setTitle] = useState(test?.title || '');
  const [description, setDescription] = useState(test?.description || '');
  const [machineMutating, setMachineMutating] = useState(test?.is_machine_mutating || false);
  const [sdkPath, setSdkPath] = useState(test?.sdk_path || '');
  const [workloadVersion, setWorkloadVersion] = useState(test?.workload_version || '');
  const [stepsText, setStepsText] = useState(
    test ? JSON.stringify(test.steps, null, 2) : JSON.stringify([{ type: 'command', command: 'dotnet --info' }], null, 2)
  );
  const [error, setError] = useState('');
  const [showHelp, setShowHelp] = useState(false);

  const toKebabCase = (s: string) =>
    s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

  const needsWorkloadVersion = stepsText.includes('{workload_version}');

  const handleBrowseSdkFolder = async () => {
    const res = await pickFolder();
    if (res.picked && res.path) {
      setSdkPath(res.path);
      if (res.has_dotnet === false) {
        alert('Selected folder does not contain a dotnet executable. Pick the SDK install root (the folder that contains dotnet.exe).');
      }
    }
  };

  const handleSave = async () => {
    if (!category.trim()) {
      setError('Category is required');
      return;
    }
    if (!title.trim()) {
      setError('Title is required');
      return;
    }

    let steps: Step[];
    try {
      steps = JSON.parse(stepsText);
    } catch {
      setError('Invalid JSON in steps');
      return;
    }

    const id = test ? test.id : toKebabCase(title);
    const payload = {
      id, category: category.trim(), title: title.trim(), description, steps,
      is_machine_mutating: machineMutating,
      sdk_path: sdkPath.trim() || null,
      workload_version: workloadVersion.trim() || null,
      is_continuous: test?.is_continuous ?? false,
    };

    try {
      if (test) {
        await updateTest(test.id, payload);
      } else {
        await createTest(payload);
      }
      onSave();
    } catch (e) {
      setError(`Failed to save: ${e}`);
    }
  };

  return (
    <div className="editor">
      <h2>{test ? 'Edit Test Case' : 'Add Test Case'}</h2>
      {error && <div className="error">{error}</div>}

      <div className="form-group">
        <label>Category <span className="required">*</span></label>
        <input value={category} onChange={e => setCategory(e.target.value)} placeholder="C# Console" required />
      </div>

      <div className="form-group">
        <label>Title <span className="required">*</span></label>
        <input value={title} onChange={e => setTitle(e.target.value)} placeholder="Test case title" required />
      </div>

      <div className="form-group">
        <label>Description</label>
        <textarea value={description} onChange={e => setDescription(e.target.value)} rows={2} />
      </div>

      <div className="form-group">
        <label>
          <input type="checkbox" checked={machineMutating} onChange={e => setMachineMutating(e.target.checked)} />
          Machine-mutating (modifies global state like workloads)
        </label>
      </div>

      <div className="form-group">
        <label>SDK folder (optional)</label>
        <div className="sdk-folder-row">
          <input
            value={sdkPath}
            onChange={e => setSdkPath(e.target.value)}
            placeholder="e.g. C:\\dotnet-zip — folder containing dotnet.exe. Blank = default (PATH) SDK."
          />
          <button type="button" className="small-btn" onClick={handleBrowseSdkFolder}>📁 Browse</button>
          {sdkPath && (
            <button type="button" className="small-btn" onClick={() => setSdkPath('')} title="Clear — use default PATH SDK">✕</button>
          )}
        </div>
        <small className="help-text">
          Pins this test to a specific SDK install (e.g. a zip-extracted SDK). Leave blank to use the machine's default (PATH) SDK.
        </small>
      </div>

      <div className="form-group">
        <label>
          Workload set version {needsWorkloadVersion && <span className="required">*</span>}
        </label>
        <input
          value={workloadVersion}
          onChange={e => setWorkloadVersion(e.target.value)}
          placeholder="e.g. 8.0.400 — leave blank unless a step uses {workload_version}"
        />
        <small className="help-text">
          Substituted for <code>{'{workload_version}'}</code> in this test's steps. Workload
          set versions are only listed on the workloads feed, so pick one from a
          <code> dotnet workload search version</code> run and paste it here before running.
        </small>
        {needsWorkloadVersion && !workloadVersion.trim() && (
          <small className="help-text">
            ⚠️ This test's steps use <code>{'{workload_version}'}</code>, so it will fail until a version is set.
          </small>
        )}
      </div>

      <div className="form-group">
        <label>Steps (JSON)</label>
        <textarea className="code-textarea" value={stepsText} onChange={e => setStepsText(e.target.value)} rows={20} />
      </div>

      <div className="form-group">
        <button className="help-toggle-btn" onClick={() => setShowHelp(!showHelp)}>
          {showHelp ? '▾ Hide Help' : '▸ Show Help & Reference'}
        </button>

        {showHelp && (
          <div className="help-panel">
            <h3>Step Format Reference</h3>
            <p>Steps are defined as a JSON array. Each step is an object with a <code>type</code> field and type-specific properties.</p>

            <h4>Step Type: <code>command</code></h4>
            <p>Executes a CLI command.</p>
            <table className="help-table">
              <thead>
                <tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr>
              </thead>
              <tbody>
                <tr><td><code>type</code></td><td>string</td><td>✓</td><td>Must be <code>"command"</code></td></tr>
                <tr><td><code>command</code></td><td>string</td><td>✓</td><td>The CLI command to run</td></tr>
                <tr><td><code>timeout</code></td><td>number</td><td></td><td>Timeout in seconds (default: 120)</td></tr>
                <tr><td><code>expected_exit_code</code></td><td>number | number[]</td><td></td><td>Expected exit code(s) (default: 0)</td></tr>
                <tr><td><code>assert_output_contains</code></td><td>string[]</td><td></td><td>Strings that must appear in stdout</td></tr>
                <tr><td><code>continue_on_error</code></td><td>boolean</td><td></td><td>If true, continue to next step even on failure</td></tr>
              </tbody>
            </table>

            <h4>Step Type: <code>write_file</code></h4>
            <p>Writes content to a file in the test working directory.</p>
            <table className="help-table">
              <thead>
                <tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr>
              </thead>
              <tbody>
                <tr><td><code>type</code></td><td>string</td><td>✓</td><td>Must be <code>"write_file"</code></td></tr>
                <tr><td><code>path</code></td><td>string</td><td>✓</td><td>Relative file path to write</td></tr>
                <tr><td><code>content</code></td><td>string</td><td>✓</td><td>File content to write</td></tr>
              </tbody>
            </table>

            <h4>Placeholders</h4>
            <p>Usable in <code>command</code>, <code>write_file</code> <code>content</code>, and <code>assert_output_contains</code>.</p>
            <table className="help-table">
              <thead>
                <tr><th>Placeholder</th><th>Expands to</th></tr>
              </thead>
              <tbody>
                <tr><td><code>{'{tfm}'}</code></td><td>Target framework moniker of the selected SDK (e.g. <code>net10.0</code>)</td></tr>
                <tr><td><code>{'{rid}'}</code></td><td>Runtime identifier (e.g. <code>win-x64</code>, <code>win-arm64</code>)</td></tr>
                <tr><td><code>{'{assets}'}</code></td><td>Path to the bundled read-only test assets folder</td></tr>
                <tr><td><code>{'{workload_version}'}</code></td><td>The "Workload set version" field above. The test fails up front if a step needs it and it is blank.</td></tr>
              </tbody>
            </table>

            <h4>Special Commands</h4>
            <ul>
              <li><code>cd &lt;directory&gt;</code> — Changes the working directory for subsequent steps (relative or absolute path)</li>
            </ul>

            <h4>Example</h4>
            <pre className="help-example">{`[
  {
    "type": "command",
    "command": "dotnet new console -o myapp",
    "timeout": 60
  },
  {
    "type": "command",
    "command": "cd myapp"
  },
  {
    "type": "write_file",
    "path": "Program.cs",
    "content": "Console.WriteLine(\\"Hello!\\");"
  },
  {
    "type": "command",
    "command": "dotnet run",
    "expected_exit_code": 0,
    "assert_output_contains": ["Hello!"]
  }
]`}</pre>

            <h4>Notes</h4>
            <ul>
              <li>Each test runs in an isolated temp directory, unless it is part of a continuous sequence (those share one directory, and are skipped once an earlier test in the sequence fails)</li>
              <li>If an SDK version is selected, a <code>global.json</code> is placed in the working directory to pin it</li>
              <li>Steps execute sequentially; execution stops at the first failure unless <code>continue_on_error</code> is set</li>
              <li>Use <code>expected_exit_code</code> as an array to accept multiple valid codes (e.g., <code>[0, 1]</code>)</li>
            </ul>
          </div>
        )}
      </div>

      <div className="form-actions">
        <button onClick={handleSave}>💾 Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
