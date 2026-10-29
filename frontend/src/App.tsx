import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { TestCase, StreamEvent, fetchTests, startExecution, cancelExecution, streamExecution, fetchRuns, fetchEnvironment, fetchSdks, SdkEntry, resolveSdkPath, deleteTest, setTestSdkPath, setTestWorkloadVersion, TestRun, fetchRunDetails, fetchDevCertStatus, openNuGetConfig } from './api'
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
import NuGetConfigPanel from './components/NuGetConfigPanel'

type View = 'dashboard' | 'tests' | 'running' | 'history_detail' | 'editor';

// Tests tabs; every tab but "all" shows the tests whose `suites` include its key.
type SuiteTab = 'all' | 'cli' | 'sanity';
const SUITE_TABS: [SuiteTab, string][] = [['all', 'All'], ['cli', 'CLI'], ['sanity', 'Sanity Check']];

const BASE_TITLE = '.NET SDK Test Runner';

/** Sentinel option value for an SDK pinned by path rather than discovered. */
const CUSTOM_SDK = '__custom_sdk__';
/** Sentinel option value that opens the path entry box. */
const PICK_SDK_PATH = '__pick_sdk_path__';

/** Steps that host or browse an HTTPS site need a trusted dev certificate. */
const HTTPS_STEP_RE = /dev-certs\s+https|https:\/\/localhost/i;

// The machine's native architecture and the dotnet host's RID differ for a
// non-native SDK (e.g. win-x86 on an x64 machine), so show both, labelled.
function archLabel(machineArch: string | null, rid: string | undefined): string {
  return `${machineArch ? ` | machine: ${machineArch}` : ''}${rid ? ` | SDK: ${rid}` : ''}`;
}

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
  const [machineArch, setMachineArch] = useState<string | null>(null);
  const [sdkLoading, setSdkLoading] = useState(false);
  const [sdkList, setSdkList] = useState<SdkEntry[]>([]);
  const [selectedSdk, setSelectedSdk] = useState<string>('');
  // A .zip or folder SDK the user pinned by path, which no installer registered
  // and so never appears in `dotnet --list-sdks` (issue #74).
  const [customSdk, setCustomSdk] = useState<{ root: string; version: string; label: string } | null>(null);
  const [sdkPathInput, setSdkPathInput] = useState('');
  const [sdkPathOpen, setSdkPathOpen] = useState(false);
  const [sdkPathBusy, setSdkPathBusy] = useState(false);
  const [sdkPathError, setSdkPathError] = useState<string | null>(null);
  // Missing NuGet feeds make restores fail with NU1102, which looks like a test
  // failure, so the config is editable from the banner (issue #64).
  const [nugetOpen, setNugetOpen] = useState(false);
  // Testers paste a full feed configuration, so the main button opens the file
  // itself in Notepad; the panel stays behind "Feeds…" (issue #76).
  const handleOpenNuGetConfig = async () => {
    try {
      const r = await openNuGetConfig();
      if (!r.ok) alert(r.error || 'Could not open NuGet.Config.');
    } catch (e) {
      alert(`Could not open NuGet.Config: ${e}`);
    }
  };
  const [runnerTests, setRunnerTests] = useState<TestCase[]>([]);
  const [selectedRun, setSelectedRun] = useState<TestRun | null>(null);
  const [runResults, setRunResults] = useState<any[]>([]);
  const [pendingRun, setPendingRun] = useState<PendingRun | null>(null);
  const [checkingCert, setCheckingCert] = useState(false);
  const [search, setSearch] = useState('');
  const [suite, setSuite] = useState<SuiteTab>('all');
  const logViewerRef = useRef<LogViewerHandle>(null);
  const headerRef = useRef<HTMLElement>(null);

  // The header is pinned, so anything else that sticks (the tests toolbar) has
  // to start below it. Its height changes with the SDK banner, so publish the
  // measured value instead of hard-coding an offset.
  useEffect(() => {
    const header = headerRef.current;
    if (!header) return;
    const publish = () => {
      document.documentElement.style.setProperty('--header-h', `${header.offsetHeight}px`);
    };
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(header);
    return () => observer.disconnect();
  }, []);

  const suiteTests = useMemo(
    () => suite === 'all' ? tests : tests.filter(t => (t.suites ?? ['cli']).includes(suite)),
    [tests, suite],
  );

  // Tests in the current tab matching the search box. Matches on what a tester
  // is likely to remember about a case: its name, what it does, and the
  // commands it runs. Every whitespace-separated term must match, so
  // "workload repair" narrows instead of widening.
  const visibleTests = useMemo(() => {
    const terms = search.toLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length === 0) return suiteTests;
    return suiteTests.filter(test => {
      const haystack = [
        test.id,
        test.title,
        test.description,
        test.category,
        ...(test.steps || []).map(s => `${s.command || ''} ${s.path || ''}`),
      ].join(' ').toLowerCase();
      return terms.every(term => haystack.includes(term));
    });
  }, [suiteTests, search]);
  const hiddenSelected = selectedIds.size - visibleTests.filter(t => selectedIds.has(t.id)).length;

  const refreshSdk = useCallback(async () => {
    setSdkLoading(true);
    try {
      const [env, sdkData] = await Promise.all([fetchEnvironment(), fetchSdks()]);
      const sdks = sdkData.sdks || [];
      setSdkList(sdks);
      // Only consider SDK "found" if dotnet --info succeeds AND at least one SDK is installed
      setSdkInfo(env.exit_code === 0 && sdks.length > 0 ? env.output : null);
      setMachineArch(env.machine_arch || null);
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
      const categoryIds = visibleTests.filter(t => t.category === category).map(t => t.id);
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
      // Select every test the search is currently showing, keeping anything
      // already selected under a previous search.
      setSelectedIds(prev => new Set([...prev, ...visibleTests.map(t => t.id)]));
    }
  }, [visibleTests]);

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
    const usingCustom = selectedSdk === CUSTOM_SDK && customSdk;
    await runTests(
      Array.from(selectedIds),
      usingCustom ? customSdk!.version : selectedSdk,
      usingCustom ? customSdk!.root : undefined,
    );
  }, [selectedIds, selectedSdk, customSdk, runTests]);

  /** Resolve the typed path (folder or .zip) and pin it as the run's SDK. */
  const handleUseSdkPath = useCallback(async () => {
    const path = sdkPathInput.trim();
    if (!path) return;
    setSdkPathBusy(true);
    setSdkPathError(null);
    try {
      const res = await resolveSdkPath(path);
      if (!res.ok || !res.root || !res.version) {
        setSdkPathError(res.error || 'could not resolve that path');
        return;
      }
      setCustomSdk({ root: res.root, version: res.version, label: res.label || res.version });
      setSelectedSdk(CUSTOM_SDK);
      setSdkPathOpen(false);
    } catch (e: any) {
      setSdkPathError(e?.message || 'could not resolve that path');
    } finally {
      setSdkPathBusy(false);
    }
  }, [sdkPathInput]);

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

  const handleSdkPathChange = useCallback(async (test: TestCase, sdkPath: string | null) => {
    await setTestSdkPath(test.id, sdkPath);
    // Re-read from the server so the editor opens with the same value.
    setTests(prev => prev.map(t => (t.id === test.id ? { ...t, sdk_path: sdkPath } : t)));
  }, []);

  const handleWorkloadVersionChange = useCallback(async (test: TestCase, version: string | null) => {
    await setTestWorkloadVersion(test.id, version);
    setTests(prev => prev.map(t => (t.id === test.id ? { ...t, workload_version: version } : t)));
  }, []);

  const handleDelete = useCallback(async (test: TestCase) => {    if (!window.confirm(`Delete test "${test.title}"?`)) return;
    try {
      await deleteTest(test.id);
      setSelectedIds(prev => { const next = new Set(prev); next.delete(test.id); return next; });
      setTests(prev => prev.filter(t => t.id !== test.id));
    } catch (e) {
      console.error('Delete failed:', e);
    }
  }, []);

  // Group tests by category
  const categories = visibleTests.reduce<Record<string, TestCase[]>>((acc, t) => {
    if (!acc[t.category]) acc[t.category] = [];
    acc[t.category].push(t);
    return acc;
  }, {});

  return (
    <div className="app">
      <header className="header" ref={headerRef}>
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
            {sdkList.length > 0 ? (
              <span className="sdk-text">
                SDK{' '}
                <select
                  className="sdk-select"
                  value={selectedSdk}
                  onChange={(e) => {
                    if (e.target.value === PICK_SDK_PATH) {
                      setSdkPathOpen(true);
                      setSdkPathError(null);
                      return;
                    }
                    setSelectedSdk(e.target.value);
                  }}
                >
                  {sdkList.map(sdk => (
                    <option key={sdk.version} value={sdk.version}>{sdk.label || sdk.version}</option>
                  ))}
                  {customSdk && (
                    <option value={CUSTOM_SDK}>{customSdk.label}</option>
                  )}
                  <option value={PICK_SDK_PATH}>📦 Enter .zip or folder path…</option>
                </select>
                {(() => {
                  if (selectedSdk === CUSTOM_SDK && customSdk) return ` | pinned: ${customSdk.root}`;
                  const osMatch = sdkInfo?.match(/OS Name:\s+(.+)/);
                  const ridMatch = sdkInfo?.match(/RID:\s+(\S+)/);
                  return ` | ${osMatch?.[1]?.trim() || 'unknown OS'}${archLabel(machineArch, ridMatch?.[1])}`;
                })()}
              </span>
            ) : (
              <span className="sdk-text">
                {(() => {
                  const versionMatch = sdkInfo?.match(/Version:\s+(\S+)/);
                  const osMatch = sdkInfo?.match(/OS Name:\s+(.+)/);
                  const ridMatch = sdkInfo?.match(/RID:\s+(\S+)/);
                  return `SDK ${versionMatch?.[1] || 'unknown'} | ${osMatch?.[1]?.trim() || 'unknown OS'}${archLabel(machineArch, ridMatch?.[1])}`;
                })()}
              </span>
            )}
            <div className="sdk-actions">
              <button className="sdk-refresh-btn" onClick={refreshSdk} disabled={sdkLoading}>
                {sdkLoading ? '⏳' : '↻'} Refresh
              </button>
              <button
                className="sdk-refresh-btn"
                onClick={handleOpenNuGetConfig}
                title="Open %APPDATA%\NuGet\NuGet.Config in Notepad"
              >
                📦 NuGet.Config
              </button>
              <button
                className="sdk-refresh-btn"
                onClick={() => setNugetOpen(true)}
                title="Check the required NuGet feeds are configured (avoids NU1102 restore errors)"
              >
                Feeds…
              </button>
            </div>
            {sdkPathOpen && (
              <div className="sdk-path-row">
                <input
                  className="sdk-path-input"
                  autoFocus
                  value={sdkPathInput}
                  placeholder="e.g. C:\dotnet-sdk-11.0.100-win-x64.zip — or a folder containing dotnet.exe"
                  onChange={e => { setSdkPathInput(e.target.value); setSdkPathError(null); }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') handleUseSdkPath();
                    if (e.key === 'Escape') setSdkPathOpen(false);
                  }}
                />
                <button className="sdk-refresh-btn" onClick={handleUseSdkPath} disabled={sdkPathBusy || !sdkPathInput.trim()}>
                  {sdkPathBusy ? '⏳ Resolving…' : 'Use'}
                </button>
                <button className="sdk-refresh-btn" onClick={() => setSdkPathOpen(false)} disabled={sdkPathBusy}>Cancel</button>
                {sdkPathBusy && <span className="sdk-path-note">Extracting a zip SDK can take a minute…</span>}
                {sdkPathError && <span className="sdk-path-error">⚠ {sdkPathError}</span>}
              </div>
            )}
          </div>
        ) : (
          <div className="sdk-banner sdk-missing">
            <span className="sdk-icon">⚠</span>
            <span className="sdk-text">No .NET SDK detected — install one and click Retry</span>
            <div className="sdk-actions">
              <button className="sdk-refresh-btn" onClick={refreshSdk} disabled={sdkLoading}>
                {sdkLoading ? '⏳' : '↻'} Retry
              </button>
              <button className="sdk-refresh-btn" onClick={handleOpenNuGetConfig}>
                📦 NuGet.Config
              </button>
              <button className="sdk-refresh-btn" onClick={() => setNugetOpen(true)}>
                Feeds…
              </button>
            </div>
          </div>
        )}
      </header>
      {nugetOpen && <NuGetConfigPanel onClose={() => setNugetOpen(false)} />}

      <main className="main">
        {view === 'dashboard' && (
          <Dashboard runs={runs} onViewRun={handleViewRun} onRetryRun={handleRetryRun} isRunning={runStatus === 'running'} />
        )}

        {view === 'tests' && (
          <div className="tests-view">
            <div className="toolbar">
              <div className="suite-tabs" role="tablist">
                {SUITE_TABS.map(([key, label]) => (
                  <button
                    key={key}
                    role="tab"
                    aria-selected={suite === key}
                    className={suite === key ? 'active' : ''}
                    onClick={() => setSuite(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="search-box">
                <span className="search-icon">🔍</span>
                <input
                  type="text"
                  className="search-input"
                  placeholder="Search tests by name, description, or command…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Escape') setSearch(''); }}
                />
                {search && (
                  <button className="search-clear" title="Clear search" onClick={() => setSearch('')}>✕</button>
                )}
              </div>
              <button onClick={() => handleSelectAll()}>Select All</button>
              <button onClick={handleDeselectAll}>Deselect All</button>
              <button className="run-btn" onClick={handleRun} disabled={selectedIds.size === 0 || runStatus === 'running' || checkingCert || pendingRun !== null}>
                {runStatus === 'running' ? '⏳ Running...' : checkingCert ? '⏳ Checking HTTPS certificate...' : `▶ Run Selected (${selectedIds.size})`}
              </button>
            </div>
            {(search || hiddenSelected > 0) && (
              <p className="search-status">
                {search && `Showing ${visibleTests.length} of ${suiteTests.length} tests`}
                {search && hiddenSelected > 0 && ' — '}
                {hiddenSelected > 0 &&
                  `${hiddenSelected} selected test${hiddenSelected > 1 ? 's are' : ' is'} hidden by the ${search ? 'search or tab' : 'tab'} and will still run`}
              </p>
            )}
            {visibleTests.length === 0 ? (
              <p className="empty-state">
                {tests.length === 0 ? 'No tests defined yet.'
                  : suiteTests.length === 0 ? 'No tests in this tab.'
                  : `No tests match "${search}".`}
              </p>
            ) : (
              <TestList
                categories={categories}
                selectedIds={selectedIds}
                onToggle={handleToggle}
                onSelectCategory={handleSelectAll}
                onEdit={(t) => { setEditingTest(t); setView('editor'); }}
                onDelete={handleDelete}
                onSdkPathChange={handleSdkPathChange}
                onWorkloadVersionChange={handleWorkloadVersionChange}
                runSdkVersion={selectedSdk === CUSTOM_SDK ? customSdk?.version : selectedSdk || undefined}
                runSdkPath={selectedSdk === CUSTOM_SDK ? customSdk?.root : undefined}
              />
            )}
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
