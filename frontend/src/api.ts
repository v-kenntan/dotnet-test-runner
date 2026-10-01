const API_BASE = '/api';

export interface TestCase {
  id: string;
  category: string;
  title: string;
  description: string;
  steps: Step[];
  is_builtin: boolean;
  is_machine_mutating: boolean;
  sdk_path?: string | null;
  /** Workload set version this test pins via the {workload_version} placeholder. */
  workload_version?: string | null;
  /** Runs as part of a continuous chain: shared working dir, skipped after a failure. */
  is_continuous?: boolean;
  /** Needs an SDK folder pinned to be meaningful, so the test list offers the picker inline. */
  requires_sdk_path?: boolean;
}

export interface Step {
  type: 'command' | 'write_file';
  command?: string;
  path?: string;
  content?: string;
  timeout?: number;
  expected_exit_code?: number | number[];
  assert_output_contains?: string[];
  continue_on_error?: boolean;
  verify_url?: string;
}

export interface TestRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  /** Omitted by the run list endpoint; only present on single-run fetches. */
  environment_info?: string;
  summary: string | null;
  sdk_version: string | null;
  sdk_path: string | null;
}

export interface StreamEvent {
  type: string;
  [key: string]: unknown;
}

export async function fetchTests(): Promise<TestCase[]> {
  const res = await fetch(`${API_BASE}/tests`);
  return res.json();
}

export async function createTest(test: Partial<TestCase>): Promise<{ id: string }> {
  const res = await fetch(`${API_BASE}/tests`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(test),
  });
  return res.json();
}

export async function updateTest(id: string, test: Partial<TestCase>): Promise<void> {
  await fetch(`${API_BASE}/tests/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(test),
  });
}

export async function deleteTest(id: string): Promise<void> {
  await fetch(`${API_BASE}/tests/${id}`, { method: 'DELETE' });
}

/** Pin (or clear, with null) one test's SDK folder without touching its steps. */
export async function setTestSdkPath(id: string, sdkPath: string | null): Promise<void> {
  const res = await fetch(`${API_BASE}/tests/${id}/sdk-path`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sdk_path: sdkPath }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || 'Failed to save SDK folder');
}

export async function startExecution(testIds: string[], sdkVersion?: string, sdkPath?: string): Promise<{ run_id: string }> {
  const res = await fetch(`${API_BASE}/execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ test_ids: testIds, sdk_version: sdkVersion || null, sdk_path: sdkPath || null }),
  });
  return res.json();
}

export async function cancelExecution(runId: string): Promise<void> {
  await fetch(`${API_BASE}/execute/${runId}/cancel`, { method: 'POST' });
}

export function streamExecution(runId: string, onEvent: (event: StreamEvent) => void): () => void {
  const eventSource = new EventSource(`${API_BASE}/execute/${runId}/stream`);
  eventSource.onmessage = (e) => {
    const event = JSON.parse(e.data);
    onEvent(event);
    if (event.type === 'run_end') {
      eventSource.close();
    }
  };
  eventSource.onerror = () => {
    eventSource.close();
  };
  return () => eventSource.close();
}

export async function fetchRuns(): Promise<TestRun[]> {
  const res = await fetch(`${API_BASE}/runs`);
  return res.json();
}

export async function fetchRunDetails(runId: string): Promise<{ run: TestRun; results: unknown[] }> {
  const res = await fetch(`${API_BASE}/runs/${runId}`);
  return res.json();
}

export async function fetchStepResults(runId: string, resultId: string): Promise<unknown[]> {
  const res = await fetch(`${API_BASE}/runs/${runId}/results/${resultId}/steps`);
  return res.json();
}

export interface RunScreenshot {
  name: string;
  url: string;
  modified: number;
  size: number;
}

export interface ResultsDestination {
  root: string;
  source: string;
  /** False when results only land on this machine and die with the VM. */
  persistent: boolean;
  env_var: string;
  /** Where the live database (and the run history in it) is kept. */
  db_path: string;
  db_source: string;
  /** False when the history dies with the VM — or with the next rebuild. */
  db_persistent: boolean;
  db_env_var: string;
}

export async function fetchResultsDestination(): Promise<ResultsDestination> {
  const res = await fetch(`${API_BASE}/results-destination`);
  return res.json();
}

export interface ExportResult {
  exported: boolean;
  path?: string;
  root?: string;
  source?: string;
  persistent?: boolean;
  screenshots?: number;
  db_copied?: boolean;
  reason?: string;
}

export async function exportRun(runId: string): Promise<ExportResult> {
  const res = await fetch(`${API_BASE}/runs/${runId}/export`, { method: 'POST' });
  return res.json();
}

export async function fetchScreenshots(runId: string): Promise<RunScreenshot[]> {
  const res = await fetch(`${API_BASE}/runs/${runId}/screenshots`);
  if (!res.ok) return [];
  return res.json();
}

export async function fetchEnvironment(): Promise<{ output: string; exit_code: number }> {
  const res = await fetch(`${API_BASE}/environment`);
  return res.json();
}

export interface SdkEntry {
  version: string;
  path: string;
  /** Settings-style name, e.g. "11.0.100-preview.7.26381.103 (x64)". */
  label?: string;
}

export interface ResolvedSdk {
  ok: boolean;
  root?: string;
  version?: string;
  label?: string;
  error?: string;
}

/** Resolve a typed SDK install folder or .zip into a usable install root. */
export async function resolveSdkPath(path: string): Promise<ResolvedSdk> {
  const res = await fetch(`${API_BASE}/sdk-path/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  return res.json();
}

export async function fetchSdks(): Promise<{ sdks: SdkEntry[]; error?: string }> {
  const res = await fetch(`${API_BASE}/sdks`);
  return res.json();
}

/** A NuGet package source, as configured or as required by an SDK. */
export interface NuGetFeed {
  key: string;
  url: string;
  protocol_version?: string;
  /** Which SDK this feed is for — one config file is shared across SDKs. */
  sdk?: string;
  present?: boolean;
  disabled?: boolean;
}

export interface NuGetConfigState {
  path: string;
  exists: boolean;
  error: string | null;
  sources: NuGetFeed[];
  required: NuGetFeed[];
  ok?: boolean;
  added?: string[];
  enabled?: string[];
  backup_path?: string | null;
}

/** Read the user-level NuGet.Config and which required feeds are missing. */
export async function fetchNuGetConfig(): Promise<NuGetConfigState> {
  const res = await fetch(`${API_BASE}/nuget-config`);
  return res.json();
}

/** Add (or re-enable) the given package sources in the user-level NuGet.Config. */
export async function addNuGetFeeds(feeds: NuGetFeed[]): Promise<NuGetConfigState> {
  const res = await fetch(`${API_BASE}/nuget-config/add`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ feeds, backup: true }),
  });
  return res.json();
}

export interface DevCertStatus {
  trusted: boolean;
  exit_code: number;
  output: string;
}

export async function fetchDevCertStatus(sdkPath?: string): Promise<DevCertStatus> {
  const query = sdkPath ? `?sdk_path=${encodeURIComponent(sdkPath)}` : '';
  const res = await fetch(`${API_BASE}/dev-certs${query}`);
  return res.json();
}

export async function trustDevCert(sdkPath?: string): Promise<DevCertStatus> {
  const res = await fetch(`${API_BASE}/dev-certs/trust`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sdk_path: sdkPath || null }),
  });
  return res.json();
}

export interface PickFolderResult {
  picked: boolean;
  path?: string;
  has_dotnet?: boolean;
  reason?: string;
}

export async function pickFolder(): Promise<PickFolderResult> {
  const res = await fetch(`${API_BASE}/pick-folder`, { method: 'POST' });
  return res.json();
}
