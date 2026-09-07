import { useState, useEffect, useCallback, useRef } from 'react'
import { TestCase, StreamEvent, fetchTests, startExecution, cancelExecution, streamExecution, fetchRuns, fetchEnvironment, fetchSdks, SdkEntry, deleteTest, TestRun, fetchRunDetails, fetchDevCertStatus } from './api'
import { resultLine } from './report'
import TestList from './components/TestList'
import TestRunner from './components/TestRunner'
import LogViewer from './components/LogViewer'
import { LogViewerHandle } from './components/LogViewer'
import ResultsSummary from './components/ResultsSummary'
import TestEditor from './components/TestEditor'
import Dashboard from './components/Dashboard'
import RunDetail from './components/RunDetail'
import DevCertPrompt from './components/DevCertPrompt'

type View = 'dashboard' | 'tests' | 'running' | 'history_detail' | 'editor';

const BASE_TITLE = '.NET SDK Test Runner';

/** Steps that host or browse an HTTPS site need a trusted dev certificate. */
const HTTPS_STEP_RE = /dev-certs\s+https|https:\/\/localhost/i;

function needsDevCert(test: TestCase): boolean {
  return (test.steps || []).some(s => HTTPS_STEP_RE.test(`${s.command || ''} ${s.verify_url || ''}`));
}

interface PendingRun {
  ids: string[];
  sdkVersion?: string;
  sdkPath?: string;
  httpsTests: TestCase[];
}

export default function App() {
  const [view, setView] = useState<View>('dashboard');
  const [tests, setTests] = useState<TestCase[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [runId, setRunId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [runStatus, setRunStatus] = useState<string>('idle');
  const [summary, setSummary] = useState<{ passed: number; failed: number; skipped: number } | null>(null);
  const [testStatuses, setTestStatuses] = useState<Record<string, string>>({});
  const [runs, setRuns] = useState<TestRun[]>([]);
  const [editingTest, setEditingTest] = useState<TestCase | null>(null);
  const [sdkInfo, setSdkInfo] = useState<string | null>(null);
  const [sdkLoading, setSdkLoading] = useState(false);
  const [sdkList, setSdkList] = useState<SdkEntry[]>([]);
  const [selectedSdk, setSelectedSdk] = useState<string>('');
  const [runnerTests, setRunnerTests] = useState<TestCase[]>([]);
  const [selectedRun, setSelectedRun] = useState<TestRun | null>(null);
  const [runResults, setRunResults] = useState<any[]>([]);
  const [pendingRun, setPendingRun] = useState<PendingRun | null>(null);
  const [checkingCert, setCheckingCert] = useState(false);
  const logViewerRef = useRef<LogViewerHandle>(null);

  const refreshSdk = useCallback(async () => {
    setSdkLoading(true);
    try {
      const [env, sdkData] = await Promise.all([fetchEnvironment(), fetchSdks()]);
      const sdks = sdkData.sdks || [];
      setSdkList(sdks);
      // Only consider SDK "found" if dotnet --info succeeds AND at least one SDK is installed
      setSdkInfo(env.exit_code === 0 && sdks.length > 0 ? env.output : null);
      if (sdks.length > 0) {
        setSelectedSdk(prev => prev || sdks[sdks.length - 1].version);
      }
    } finally {
      setSdkLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTests().then(setTests);
    fetchRuns().then(setRuns);
    refreshSdk();
  }, []);

  // Refresh runs whenever navigating back to dashboard
  useEffect(() => {
    if (view === 'dashboard') {
      fetchRuns().then(setRuns);
    }
  }, [view]);

  const handleSelectAll = useCallback((category?: string) => {
    if (category) {
      // Toggle all in this category only
      const categoryIds = tests.filter(t => t.category === category).map(t => t.id);
      setSelectedIds(prev => {
        const allSelected = categoryIds.every(id => prev.has(id));
        const next = new Set(prev);
        if (allSelected) {
          categoryIds.forEach(id => next.delete(id));
        } else {
          categoryIds.forEach(id => next.add(id));
        }
        return next;
      });
    } else {
      // Select all tests
      setSelectedIds(new Set(tests.map(t => t.id)));
    }
  }, [tests]);

  const handleDeselectAll = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const handleToggle = useCallback((id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const startRun = useCallback(async (ids: string[], sdkVersion?: string, sdkPath?: string) => {
    setLogs([]);
    setSummary(null);
    setTestStatuses({});
    setRunStatus('running');
    setRunnerTests(tests.filter(t => ids.includes(t.id)));
    setView('running');
    document.title = `⏳ Running — ${BASE_TITLE}`;

    const { run_id } = await startExecution(ids, sdkVersion, sdkPath);
    setRunId(run_id);

    streamExecution(run_id, (event: StreamEvent) => {
      switch (event.type) {
        case 'run_start':
          setLogs(prev => [...prev, `▶ Run started (${event.total_tests} tests)`]);
          break;
        case 'test_start':
          setLogs(prev => [...prev, `\n━━━ ${event.title} ━━━`]);
          setTestStatuses(prev => ({ ...prev, [event.test_case_id as string]: 'running' }));
          break;
        case 'step_output':
          setLogs(prev => [...prev, event.line as string]);
          break;
        case 'test_end':
          setTestStatuses(prev => ({ ...prev, [event.test_case_id as string]: event.status as string }));
          setLogs(prev => [...prev, resultLine(event.status as string)]);
          break;
        case 'run_end': {
          setRunStatus('completed');
          setSummary(event.summary as { passed: number; failed: number; skipped: number });
          setLogs(prev => [...prev, `\n✅ Run complete: ${JSON.stringify(event.summary)}`]);
          // The window is often buried behind a test's Notepad/browser window,
          // so mark completion in the title too (issue #67 — the backend also
          // pops a foreground message box).
          const cancelled = event.status === 'cancelled';
          const failed = (event.summary as { failed: number }).failed;
          const icon = cancelled ? '⛔' : failed ? '❌' : '✅';
          document.title = `${icon} Run ${cancelled ? 'cancelled' : 'complete'} — ${BASE_TITLE}`;
          break;
        }
      }
    });
  }, [tests]);

  // Gate: tests that host HTTPS sites (test cases 4 and 9) pop a Windows trust
  // dialog mid-run when no dev certificate is trusted. Prompt for it up front.
  const runTests = useCallback(async (ids: string[], sdkVersion?: string, sdkPath?: string) => {
    const httpsTests = tests.filter(t => ids.includes(t.id) && needsDevCert(t));
    if (httpsTests.length > 0) {
      setCheckingCert(true);
      const status = await fetchDevCertStatus(sdkPath).catch(() => null);
      setCheckingCert(false);
      if (status && !status.trusted) {
        setPendingRun({ ids, sdkVersion, sdkPath, httpsTests });
        return;
      }
    }
    await startRun(ids, sdkVersion, sdkPath);
  }, [tests, startRun]);

  const handleRun = useCallback(async () => {
    if (selectedIds.size === 0) return;
    await runTests(Array.from(selectedIds), selectedSdk);
  }, [selectedIds, selectedSdk, runTests]);

  const handleCancel = useCallback(async () => {
    if (runId) {
      await cancelExecution(runId);
      setRunStatus('cancelled');
    }
  }, [runId]);

  const handleViewRun = useCallback(async (run: TestRun) => {
    const { results } = await fetchRunDetails(run.id);
    // Enrich results with test titles
    const enrichedResults = (results as any[]).map(r => {
      const test = tests.find(t => t.id === r.test_case_id);
      return { ...r, title: test?.title || r.test_case_id, category: test?.category || '' };
    });
    setSelectedRun(run);
    setRunResults(enrichedResults);
    setView('history_detail');
  }, [tests]);

  const handleRetryRun = useCallback(async (run: TestRun) => {
    if (runStatus === 'running' || checkingCert || pendingRun) return;
    const { results } = await fetchRunDetails(run.id);
    const testIds = (results as any[]).map((r: any) => r.test_case_id as string);
    if (testIds.length === 0) return;

    // Check for deleted tests
    const availableIds = testIds.filter(id => tests.some(t => t.id === id));
    const missingCount = testIds.length - availableIds.length;

    if (availableIds.length === 0) {
      alert('All tests from this run have been deleted. Cannot retry.');
      return;
    }

    if (missingCount > 0) {
      const proceed = window.confirm(
        `${missingCount} test${missingCount > 1 ? 's' : ''} from this run ha${missingCount > 1 ? 've' : 's'} been deleted. Continue with the remaining ${availableIds.length} test${availableIds.length > 1 ? 's' : ''}?`
      );
      if (!proceed) return;
    }

    const sdkVersion = run.sdk_version || selectedSdk;
    await runTests(availableIds, sdkVersion, run.sdk_path || undefined);
  }, [runStatus, checkingCert, pendingRun, selectedSdk, tests, runTests]);

  const handleRefreshTests = useCallback(async () => {
    const data = await fetchTests();
    setTests(data);
  }, []);

  const handleDelete = useCallback(async (test: TestCase) => {
    if (!window.confirm(`Delete test "${test.title}"?`)) return;
    try {
      await deleteTest(test.id);
      setSelectedIds(prev => { const next = new Set(prev); next.delete(test.id); return next; });
      setTests(prev => prev.filter(t => t.id !== test.id));
    } catch (e) {
      console.error('Delete failed:', e);
    }
  }, []);

  // Group tests by category
  const categories = tests.reduce<Record<string, TestCase[]>>((acc, t) => {
    if (!acc[t.category]) acc[t.category] = [];
    acc[t.category].push(t);
    return acc;
  }, {});

  return (
    <div className="app">
      <header className="header">
        <div className="header-top">
          <h1>🧪 .NET SDK Test Runner</h1>
          <nav>
            <button className={view === 'dashboard' ? 'active' : ''} onClick={() => setView('dashboard')}>Dashboard</button>
            <button className={view === 'tests' ? 'active' : ''} onClick={() => setView('tests')}>Tests</button>
            <button className={view === 'running' ? 'active' : ''} onClick={() => setView('running')}>Runner</button>
            <button className={view === 'editor' ? 'active' : ''} onClick={() => { setEditingTest(null); setView('editor'); }}>+ Add Test</button>
          </nav>
        </div>
        {(sdkInfo !== null || sdkList.length > 0) ? (
          <div className="sdk-banner sdk-found">
            <span className="sdk-icon">✓</span>
            {sdkList.length > 1 ? (
              <span className="sdk-text">
                SDK{' '}
                <select
                  className="sdk-select"
                  value={selectedSdk}
                  onChange={(e) => setSelectedSdk(e.target.value)}
                >
                  {sdkList.map(sdk => (
                    <option key={sdk.version} value={sdk.version}>{sdk.version}</option>
                  ))}
                </select>
                {(() => {
                  const osMatch = sdkInfo?.match(/OS Name:\s+(.+)/);
                  const ridMatch = sdkInfo?.match(/RID:\s+(\S+)/);
                  return ` | ${osMatch?.[1]?.trim() || 'unknown OS'} | ${ridMatch?.[1] || ''}`;
                })()}
              </span>
            ) : (
              <span className="sdk-text">
                {(() => {
                  const versionMatch = sdkInfo?.match(/Version:\s+(\S+)/);
                  const osMatch = sdkInfo?.match(/OS Name:\s+(.+)/);
                  const ridMatch = sdkInfo?.match(/RID:\s+(\S+)/);
                  return `SDK ${versionMatch?.[1] || 'unknown'} | ${osMatch?.[1]?.trim() || 'unknown OS'} | ${ridMatch?.[1] || ''}`;
                })()}
              </span>
            )}
            <button className="sdk-refresh-btn" onClick={refreshSdk} disabled={sdkLoading}>
              {sdkLoading ? '⏳' : '↻'} Refresh
            </button>
          </div>
        ) : (
          <div className="sdk-banner sdk-missing">
            <span className="sdk-icon">⚠</span>
            <span className="sdk-text">No .NET SDK detected — install one and click Retry</span>
            <button className="sdk-refresh-btn" onClick={refreshSdk} disabled={sdkLoading}>
              {sdkLoading ? '⏳' : '↻'} Retry
            </button>
          </div>
        )}
      </header>

      <main className="main">
        {view === 'dashboard' && (
          <Dashboard runs={runs} onViewRun={handleViewRun} onRetryRun={handleRetryRun} isRunning={runStatus === 'running'} />
        )}

        {view === 'tests' && (
          <div className="tests-view">
            <div className="toolbar">
              <button onClick={() => handleSelectAll()}>Select All</button>
              <button onClick={handleDeselectAll}>Deselect All</button>
              <button className="run-btn" onClick={handleRun} disabled={selectedIds.size === 0 || runStatus === 'running' || checkingCert || pendingRun !== null}>
                {runStatus === 'running' ? '⏳ Running...' : checkingCert ? '⏳ Checking HTTPS certificate...' : `▶ Run Selected (${selectedIds.size})`}
              </button>
            </div>
            <TestList
              categories={categories}
              selectedIds={selectedIds}
              onToggle={handleToggle}
              onSelectCategory={handleSelectAll}
              onEdit={(t) => { setEditingTest(t); setView('editor'); }}
              onDelete={handleDelete}
            />
          </div>
        )}

        {view === 'running' && (
          <TestRunner
            status={runStatus}
            testStatuses={testStatuses}
            tests={runnerTests}
            onCancel={handleCancel}
            onTestClick={(title) => logViewerRef.current?.scrollToTest(title)}
          >
            <LogViewer ref={logViewerRef} logs={logs} />
            {summary && <ResultsSummary summary={summary} logs={logs} testStatuses={testStatuses} tests={runnerTests} />}
          </TestRunner>
        )}

        {view === 'history_detail' && selectedRun && (
          <RunDetail run={selectedRun} results={runResults} tests={tests} onBack={() => setView('dashboard')} />
        )}

        {view === 'editor' && (
          <TestEditor
            test={editingTest}
            onSave={() => { handleRefreshTests(); setView('tests'); }}
            onCancel={() => setView('tests')}
          />
        )}
      </main>

      {pendingRun && (
        <DevCertPrompt
          tests={pendingRun.httpsTests}
          sdkPath={pendingRun.sdkPath}
          onProceed={() => {
            const { ids, sdkVersion, sdkPath } = pendingRun;
            setPendingRun(null);
            startRun(ids, sdkVersion, sdkPath);
          }}
          onCancel={() => setPendingRun(null)}
        />
      )}
    </div>
  );
}
